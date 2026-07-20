"""Exercises admin_app.py's _tick() state machine and settings routes
against a temp DATA_DIR, with pco_client/generate_static_site calls
monkeypatched out -- no real network, session, or subprocess involved."""

from datetime import datetime, timedelta, timezone

import pytest

import admin_app
import scheduler


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point every module-level constant admin_app.py reads at a temp dir,
    and make _regenerate a no-op that just writes a plausible site + sidecar
    (avoids invoking the real subprocess/network path in generate_static_site.py)."""
    monkeypatch.setattr(admin_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(admin_app, "RULE_STORE", scheduler.RuleStore(tmp_path))
    monkeypatch.setattr(admin_app, "TITLE_PREFIX", "Test Church")

    def fake_regenerate(data_dir, service_type_id=None, plan_id=None, title_prefix=None):
        p = admin_app._paths(data_dir)
        p["site"].parent.mkdir(parents=True, exist_ok=True)
        p["site"].write_text("<html>fake site</html>", encoding="utf-8")
        p["site_plan"].write_text(
            '{"service_type_id": "%s", "plan_id": "%s", "plan_title": "Fake Plan", "plan_date": "2026-07-26"}'
            % (service_type_id or "804383", plan_id or "p1"),
            encoding="utf-8",
        )

    monkeypatch.setattr(admin_app, "_regenerate", fake_regenerate)
    yield tmp_path


def _healthy_evaluation(rule_id, service_type_id, now):
    return scheduler.RuleEvaluation(
        rule_id=rule_id,
        ok=True,
        reason="ok",
        service_type_id=service_type_id,
        plan_id="p1",
        plan_title="Sunday Service",
        window_starts_at=now - timedelta(minutes=5),
        window_ends_at=now + timedelta(hours=2),
    )


def _failing_evaluation(rule_id, service_type_id, reason="plan has 0 songs"):
    return scheduler.RuleEvaluation(rule_id=rule_id, ok=False, reason=reason, service_type_id=service_type_id)


# --------------------------------------------------------------------------
# Guardrail pass/fail
# --------------------------------------------------------------------------


def test_tick_auto_opens_on_healthy_plan(tmp_path, monkeypatch):
    rule = admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(admin_app, "evaluate_rule", lambda *a, **k: _healthy_evaluation(rule.id, "804383", now))

    admin_app._tick(tmp_path)

    assert admin_app._read_state(tmp_path) == "open"
    open_plan = scheduler.read_open_plan(tmp_path)
    assert open_plan is not None
    assert open_plan.opened_by == "automation"
    assert open_plan.rule_id == rule.id

    updated_rule = admin_app.RULE_STORE.get(rule.id)
    assert updated_rule.last_action == "opened"


def test_tick_stays_closed_and_logs_reason_on_guardrail_failure(tmp_path, monkeypatch):
    rule = admin_app.RULE_STORE.add("979578", "Special Events")
    monkeypatch.setattr(admin_app, "evaluate_rule", lambda *a, **k: _failing_evaluation(rule.id, "979578"))

    admin_app._tick(tmp_path)

    assert admin_app._read_state(tmp_path) == "closed"
    assert scheduler.read_open_plan(tmp_path) is None
    updated_rule = admin_app.RULE_STORE.get(rule.id)
    assert updated_rule.last_action == "skipped"
    assert updated_rule.last_reason == "plan has 0 songs"


def test_tick_disabled_rule_is_never_evaluated(tmp_path, monkeypatch):
    rule = admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")
    admin_app.RULE_STORE.set_enabled(rule.id, False)

    def boom(*a, **k):
        raise AssertionError("evaluate_rule should not be called for a disabled rule")

    monkeypatch.setattr(admin_app, "evaluate_rule", boom)
    admin_app._tick(tmp_path)  # must not raise
    assert admin_app._read_state(tmp_path) == "closed"


def test_tick_healthy_plan_but_not_yet_in_window_does_not_open(tmp_path, monkeypatch):
    rule = admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")
    now = datetime.now(timezone.utc)
    future_window = scheduler.RuleEvaluation(
        rule_id=rule.id,
        ok=True,
        reason="ok",
        service_type_id="804383",
        plan_id="p1",
        plan_title="Sunday Service",
        window_starts_at=now + timedelta(hours=1),
        window_ends_at=now + timedelta(hours=4),
    )
    monkeypatch.setattr(admin_app, "evaluate_rule", lambda *a, **k: future_window)

    admin_app._tick(tmp_path)

    assert admin_app._read_state(tmp_path) == "closed"
    updated_rule = admin_app.RULE_STORE.get(rule.id)
    assert updated_rule.last_action == "waiting"


# --------------------------------------------------------------------------
# Manual vs automation interaction
# --------------------------------------------------------------------------


def test_manual_open_blocks_automation_from_touching_it(tmp_path, monkeypatch):
    admin_app._apply_state(tmp_path, "closed")
    admin_app._write_state(tmp_path, "closed")
    # Simulate a manual open: site/index.html + sidecar exist (as if
    # Regenerate had been clicked), then a human clicks Open.
    admin_app._regenerate(tmp_path)
    with admin_app.app.test_client() as client:
        resp = client.post("/open", headers=_auth_header())
        assert resp.status_code in (302, 303)

    open_plan = scheduler.read_open_plan(tmp_path)
    assert open_plan is not None
    assert open_plan.opened_by == "manual"

    rule = admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")

    def boom(*a, **k):
        raise AssertionError("automation must not evaluate rules while the site is already open")

    monkeypatch.setattr(admin_app, "evaluate_rule", boom)
    admin_app._tick(tmp_path)  # must not raise, must not touch anything
    assert admin_app._read_state(tmp_path) == "open"
    assert scheduler.read_open_plan(tmp_path).opened_by == "manual"


def test_auto_close_only_fires_for_automation_opened_windows(tmp_path):
    admin_app._apply_state(tmp_path, "closed")
    admin_app._write_state(tmp_path, "closed")
    admin_app._regenerate(tmp_path)
    admin_app._apply_state(tmp_path, "open")
    admin_app._write_state(tmp_path, "open")
    past_ends_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    scheduler.write_open_plan(
        tmp_path,
        scheduler.OpenPlan(
            service_type_id="804383",
            plan_id="p1",
            plan_title="Sunday Service",
            opened_by="manual",
            window_ends_at=past_ends_at,
        ),
    )

    admin_app._tick(tmp_path)

    # A manually-opened window with a (theoretically) elapsed end time must
    # NOT be auto-closed -- only automation-opened windows are.
    assert admin_app._read_state(tmp_path) == "open"


def test_restart_safety_auto_closes_elapsed_automation_window(tmp_path):
    """Simulates a container restart mid-service: open_plan.json survives on
    disk (it's on DATA_DIR), and the next tick after restart should close it
    if its window has since ended -- it shouldn't need to wait a full
    poll interval."""
    admin_app._apply_state(tmp_path, "closed")
    admin_app._write_state(tmp_path, "closed")
    admin_app._regenerate(tmp_path)
    admin_app._apply_state(tmp_path, "open")
    admin_app._write_state(tmp_path, "open")
    past_ends_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    scheduler.write_open_plan(
        tmp_path,
        scheduler.OpenPlan(
            service_type_id="804383",
            plan_id="p1",
            plan_title="Sunday Service",
            opened_by="automation",
            rule_id="rule1",
            window_ends_at=past_ends_at,
        ),
    )

    admin_app._tick(tmp_path)

    assert admin_app._read_state(tmp_path) == "closed"
    assert scheduler.read_open_plan(tmp_path) is None


def test_self_heals_stale_open_plan_when_closed(tmp_path):
    scheduler.write_open_plan(
        tmp_path,
        scheduler.OpenPlan(
            service_type_id="804383", plan_id="p1", plan_title="Stale", opened_by="automation"
        ),
    )
    admin_app._write_state(tmp_path, "closed")

    admin_app._tick(tmp_path)

    assert scheduler.read_open_plan(tmp_path) is None


# --------------------------------------------------------------------------
# Settings routes (happy path)
# --------------------------------------------------------------------------


def _auth_header():
    import base64

    token = base64.b64encode(f"{admin_app.ADMIN_USERNAME}:test-password".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(autouse=True)
def _admin_password(monkeypatch):
    monkeypatch.setattr(admin_app, "ADMIN_PASSWORD", "test-password")


def test_settings_page_lists_rules(monkeypatch):
    monkeypatch.setattr(admin_app, "list_service_types", lambda session: [
        {"id": "804383", "attributes": {"name": "Söndagsgudstjänst"}}
    ])
    admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")
    with admin_app.app.test_client() as client:
        resp = client.get("/settings", headers=_auth_header())
        assert resp.status_code == 200
        assert b"S\xc3\xb6ndagsgudstj\xc3\xa4nst" in resp.data


def test_add_rule_route_creates_a_rule(monkeypatch):
    monkeypatch.setattr(admin_app, "list_service_types", lambda session: [
        {"id": "979578", "attributes": {"name": "Special Events"}}
    ])
    with admin_app.app.test_client() as client:
        resp = client.post(
            "/settings/rules", data={"service_type_id": "979578", "title_prefix": ""}, headers=_auth_header()
        )
        assert resp.status_code in (302, 303)

    rules = admin_app.RULE_STORE.load()
    assert len(rules) == 1
    assert rules[0].service_type_id == "979578"
    assert rules[0].service_type_name == "Special Events"


def test_toggle_and_delete_rule_routes():
    rule = admin_app.RULE_STORE.add("804383", "Söndagsgudstjänst")
    with admin_app.app.test_client() as client:
        client.post(f"/settings/rules/{rule.id}/toggle", headers=_auth_header())
        assert admin_app.RULE_STORE.get(rule.id).enabled is False

        client.post(f"/settings/rules/{rule.id}/delete", headers=_auth_header())
        assert admin_app.RULE_STORE.get(rule.id) is None
