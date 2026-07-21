from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import scheduler
from pco_client import PlanningCenterError

TZ = ZoneInfo("Europe/Stockholm")


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# --------------------------------------------------------------------------
# _is_sane_window
# --------------------------------------------------------------------------


def test_is_sane_window_rejects_zero_duration():
    starts = _dt("2025-09-12T18:13:03+00:00")
    ok, reason = scheduler._is_sane_window(starts, starts, date(2025, 9, 12), TZ, 15, 720)
    assert not ok
    assert "duration" in reason


def test_is_sane_window_rejects_too_short():
    starts = _dt("2025-09-06T12:00:11+00:00")
    ends = starts + timedelta(minutes=5)
    ok, reason = scheduler._is_sane_window(starts, ends, date(2025, 9, 6), TZ, 15, 720)
    assert not ok
    assert "too short" in reason


def test_is_sane_window_rejects_too_long():
    starts = _dt("2025-08-09T08:30:50+00:00")
    ends = starts + timedelta(hours=20)
    ok, reason = scheduler._is_sane_window(starts, ends, date(2025, 8, 9), TZ, 15, 720)
    assert not ok
    assert "too long" in reason


def test_is_sane_window_rejects_date_mismatch():
    starts = _dt("2026-07-26T14:00:00+00:00")  # 2026-07-26 16:00 local (CEST)
    ends = starts + timedelta(hours=3)
    ok, reason = scheduler._is_sane_window(starts, ends, date(2026, 7, 27), TZ, 15, 720)
    assert not ok
    assert "expected" in reason


def test_is_sane_window_accepts_real_sunday_window():
    starts = _dt("2026-07-26T14:00:00+00:00")
    ends = _dt("2026-07-26T17:00:00+00:00")
    ok, reason = scheduler._is_sane_window(starts, ends, date(2026, 7, 26), TZ, 15, 720)
    assert ok
    assert reason == ""


def test_is_sane_window_accepts_long_legitimate_event():
    # e.g. "King's Kids Huskvarna": a real all-day event, not a placeholder.
    starts = _dt("2025-08-09T08:30:50+00:00")
    ends = _dt("2025-08-09T20:00:50+00:00")
    ok, _ = scheduler._is_sane_window(starts, ends, date(2025, 8, 9), TZ, 15, 720)
    assert ok


# --------------------------------------------------------------------------
# _select_service_plan_time
# --------------------------------------------------------------------------


def test_select_service_plan_time_empty():
    selected, reason = scheduler._select_service_plan_time([])
    assert selected is None
    assert "no scheduled times" in reason


def test_select_service_plan_time_prefers_service_tag():
    rows = [
        {"attributes": {"time_type": "rehearsal", "starts_at": "2026-07-26T12:00:00Z"}},
        {"attributes": {"time_type": "service", "starts_at": "2026-07-26T14:00:00Z"}},
    ]
    selected, _ = scheduler._select_service_plan_time(rows)
    assert selected["attributes"]["time_type"] == "service"


def test_select_service_plan_time_falls_back_when_none_tagged_service():
    rows = [
        {"attributes": {"time_type": "other", "starts_at": "2026-07-26T14:00:00Z"}},
    ]
    selected, _ = scheduler._select_service_plan_time(rows)
    assert selected is not None


def test_select_service_plan_time_breaks_ties_by_earliest_start():
    rows = [
        {"attributes": {"time_type": "service", "starts_at": "2026-07-26T16:00:00Z"}},
        {"attributes": {"time_type": "service", "starts_at": "2026-07-26T09:00:00Z"}},
    ]
    selected, _ = scheduler._select_service_plan_time(rows)
    assert selected["attributes"]["starts_at"] == "2026-07-26T09:00:00Z"


# --------------------------------------------------------------------------
# evaluate_rule -- monkeypatch the pco_client calls scheduler.py imported
# directly, so no network/session is needed.
# --------------------------------------------------------------------------


def _rule() -> scheduler.Rule:
    return scheduler.Rule.new("804383", "Söndagsgudstjänst")


def test_evaluate_rule_no_plan_today(monkeypatch):
    def raise_not_found(session, target, service_type_id):
        raise PlanningCenterError("no plan")

    monkeypatch.setattr(scheduler, "find_plan_by_date", raise_not_found)
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert not result.ok
    assert "no plan found" in result.reason


def test_evaluate_rule_zero_songs(monkeypatch):
    plan = {"id": "p1", "attributes": {"title": "Test"}}
    monkeypatch.setattr(scheduler, "find_plan_by_date", lambda *a, **k: ("804383", plan))
    monkeypatch.setattr(scheduler, "get_plan_items", lambda *a, **k: [])
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert not result.ok
    assert "0 songs" in result.reason


def test_evaluate_rule_no_plan_times(monkeypatch):
    plan = {"id": "p1", "attributes": {"title": "Test"}}
    items = [{"attributes": {"item_type": "song"}}]
    monkeypatch.setattr(scheduler, "find_plan_by_date", lambda *a, **k: ("804383", plan))
    monkeypatch.setattr(scheduler, "get_plan_items", lambda *a, **k: items)
    monkeypatch.setattr(scheduler, "get_plan_times", lambda *a, **k: [])
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert not result.ok
    assert "no scheduled times" in result.reason


def test_evaluate_rule_degenerate_window(monkeypatch):
    plan = {"id": "p1", "attributes": {"title": "Lovsång hemma"}}
    items = [{"attributes": {"item_type": "song"}}]
    plan_times = [{"attributes": {
        "time_type": "service",
        "starts_at": "2026-07-26T14:00:00Z",
        "ends_at": "2026-07-26T14:00:00Z",
    }}]
    monkeypatch.setattr(scheduler, "find_plan_by_date", lambda *a, **k: ("804383", plan))
    monkeypatch.setattr(scheduler, "get_plan_items", lambda *a, **k: items)
    monkeypatch.setattr(scheduler, "get_plan_times", lambda *a, **k: plan_times)
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert not result.ok
    assert "duration" in result.reason


def test_evaluate_rule_healthy_plan(monkeypatch):
    plan = {"id": "p1", "attributes": {"title": "Sunday Service"}}
    items = [{"attributes": {"item_type": "song"}} for _ in range(5)]
    plan_times = [{"attributes": {
        "time_type": "service",
        "starts_at": "2026-07-26T14:00:00Z",
        "ends_at": "2026-07-26T17:00:00Z",
    }}]
    monkeypatch.setattr(scheduler, "find_plan_by_date", lambda *a, **k: ("804383", plan))
    monkeypatch.setattr(scheduler, "get_plan_items", lambda *a, **k: items)
    monkeypatch.setattr(scheduler, "get_plan_times", lambda *a, **k: plan_times)
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert result.ok
    assert result.plan_id == "p1"
    assert result.plan_title == "Sunday Service"
    assert result.window_starts_at == datetime(2026, 7, 26, 14, 0, tzinfo=timezone.utc)
    assert result.window_ends_at == datetime(2026, 7, 26, 17, 0, tzinfo=timezone.utc)


def test_evaluate_rule_one_bad_rule_does_not_raise(monkeypatch):
    """evaluate_rule must never raise -- a caller loops over several rules
    and one bad API response must not block evaluating the rest."""

    def boom(*a, **k):
        raise PlanningCenterError("network blip")

    monkeypatch.setattr(scheduler, "find_plan_by_date", boom)
    result = scheduler.evaluate_rule(None, _rule(), date(2026, 7, 26), TZ)
    assert result.ok is False


# --------------------------------------------------------------------------
# RuleStore
# --------------------------------------------------------------------------


def test_rule_store_round_trip(tmp_path):
    store = scheduler.RuleStore(tmp_path)
    assert store.load() == []

    rule = store.add("804383", "Söndagsgudstjänst")
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].id == rule.id
    assert loaded[0].enabled is True

    store.set_enabled(rule.id, False)
    assert store.load()[0].enabled is False

    store.update_bookkeeping(rule.id, last_action="skipped", last_reason="plan has 0 songs")
    updated = store.get(rule.id)
    assert updated.last_action == "skipped"
    assert updated.last_reason == "plan has 0 songs"

    store.delete(rule.id)
    assert store.load() == []


# --------------------------------------------------------------------------
# open_plan.json
# --------------------------------------------------------------------------


def test_open_plan_round_trip(tmp_path):
    assert scheduler.read_open_plan(tmp_path) is None

    open_plan = scheduler.OpenPlan(
        service_type_id="804383",
        plan_id="p1",
        plan_title="Sunday Service",
        opened_by="automation",
        rule_id="rule1",
        window_ends_at="2026-07-26T17:00:00+00:00",
    )
    scheduler.write_open_plan(tmp_path, open_plan)
    loaded = scheduler.read_open_plan(tmp_path)
    assert loaded == open_plan

    scheduler.clear_open_plan(tmp_path)
    assert scheduler.read_open_plan(tmp_path) is None


def test_read_open_plan_handles_corrupt_file(tmp_path):
    path = tmp_path / "open_plan.json"
    path.write_text("not json", encoding="utf-8")
    assert scheduler.read_open_plan(tmp_path) is None
