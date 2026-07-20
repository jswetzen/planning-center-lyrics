#!/usr/bin/env python3
"""
scheduler.py

Rule-based automation for admin_app.py: decides, for each configured Rule
(one Planning Center service type), whether today's plan should be
auto-opened right now -- based on the plan's actual scheduled time
(plan_times), with a guardrail that skips (and records why) rather than
acting on a plan that's missing, empty, or has an implausible time window.

Deliberately has no Flask/HTTP/threading in it: this module is pure logic
plus JSON persistence, so it's testable without a running server or network
access. admin_app.py owns the background thread, the open/closed state
machine, and turning a "guardrail passed, window is [starts_at, ends_at)"
result into an actual open/close action.

Persisted on the same DATA_DIR volume admin_app.py already uses:
    <DATA_DIR>/rules.json       configured rules + last-evaluated bookkeeping
    <DATA_DIR>/open_plan.json   which plan (if any) is currently live, and
                                whether a human or automation opened it
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import requests

from pco_client import (
    PlanningCenterError,
    find_plan_by_date,
    get_plan_items,
    get_plan_times,
    parse_pco_datetime,
)

log = logging.getLogger("scheduler")

DEFAULT_MIN_WINDOW_MINUTES = 15
DEFAULT_MAX_WINDOW_MINUTES = 12 * 60  # 12 hours -- generous on purpose, see _is_sane_window.


# --------------------------------------------------------------------------
# Rule config + bookkeeping (persisted at <DATA_DIR>/rules.json)
# --------------------------------------------------------------------------


@dataclass
class Rule:
    id: str
    service_type_id: str
    service_type_name: str
    enabled: bool = True
    title_prefix: Optional[str] = None
    min_window_minutes: int = DEFAULT_MIN_WINDOW_MINUTES
    max_window_minutes: int = DEFAULT_MAX_WINDOW_MINUTES

    # Bookkeeping only -- what the scheduler last saw for this rule, for
    # display on the settings screen. Never read back to make decisions.
    last_checked_at: Optional[str] = None
    last_action: Optional[str] = None  # "opened" | "skipped" | "waiting" | "error" | None
    last_plan_title: Optional[str] = None
    last_window_starts_at: Optional[str] = None
    last_window_ends_at: Optional[str] = None
    last_reason: Optional[str] = None

    @staticmethod
    def new(service_type_id: str, service_type_name: str, title_prefix: Optional[str] = None) -> "Rule":
        return Rule(
            id=uuid.uuid4().hex[:12],
            service_type_id=service_type_id,
            service_type_name=service_type_name,
            title_prefix=title_prefix,
        )


class RuleStore:
    """JSON-backed list of Rules at <data_dir>/rules.json. Writes are atomic
    (tmp file + os.replace) since this is hand-authored config worth
    protecting from a crash mid-write, unlike the trivially-regenerable
    site output."""

    def __init__(self, data_dir: Path):
        self._path = data_dir / "rules.json"

    def load(self) -> list[Rule]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [Rule(**r) for r in raw]

    def save(self, rules: list[Rule]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps([asdict(r) for r in rules], indent=2), encoding="utf-8")
        os.replace(tmp_path, self._path)

    def get(self, rule_id: str) -> Optional[Rule]:
        return next((r for r in self.load() if r.id == rule_id), None)

    def add(self, service_type_id: str, service_type_name: str, title_prefix: Optional[str] = None) -> Rule:
        rules = self.load()
        rule = Rule.new(service_type_id, service_type_name, title_prefix)
        rules.append(rule)
        self.save(rules)
        return rule

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        rules = self.load()
        for r in rules:
            if r.id == rule_id:
                r.enabled = enabled
        self.save(rules)

    def delete(self, rule_id: str) -> None:
        self.save([r for r in self.load() if r.id != rule_id])

    def update_bookkeeping(self, rule_id: str, **fields) -> None:
        rules = self.load()
        for r in rules:
            if r.id == rule_id:
                for key, value in fields.items():
                    setattr(r, key, value)
        self.save(rules)


# --------------------------------------------------------------------------
# Guardrail: is today's plan real enough to trust?
# --------------------------------------------------------------------------


def _select_service_plan_time(plan_times: list[dict]) -> tuple[Optional[dict], str]:
    """Pick the plan_times row to trust.

    Prefers rows tagged time_type=='service' -- but on this account every
    observed row is tagged 'service' regardless of whether the window is
    real or a leftover placeholder, so this is a soft preference (falls
    back to all rows if none match), not a hard filter. The actual signal
    is the duration/date sanity check in _is_sane_window. Ties among
    same-tagged rows are broken by earliest starts_at.
    """
    if not plan_times:
        return None, "plan has no scheduled times"

    tagged = [t for t in plan_times if t["attributes"].get("time_type") == "service"]
    candidates = tagged if tagged else plan_times
    candidates = sorted(candidates, key=lambda t: t["attributes"].get("starts_at") or "")
    return candidates[0], ""


def _is_sane_window(
    starts_at: datetime,
    ends_at: datetime,
    expected_local_date: date,
    tz: ZoneInfo,
    min_minutes: int,
    max_minutes: int,
) -> tuple[bool, str]:
    """Sanity-check a plan_times window against known-bad patterns.

    Real historical data on this account includes an exact 0-duration
    placeholder window right next to a legitimate 11.5-hour all-day event in
    the *same* service type -- so thresholds are deliberately generous and
    per-rule configurable rather than one tight global window. What they
    reliably catch: exact/near-zero-duration placeholders, and a plan_times
    row left over from a different day than the plan it's attached to.
    """
    duration = ends_at - starts_at
    if duration <= timedelta(0):
        return False, f"non-positive duration ({duration})"
    if duration < timedelta(minutes=min_minutes):
        return False, f"window too short ({duration}, minimum {min_minutes}min)"
    if duration > timedelta(minutes=max_minutes):
        return False, f"window too long ({duration}, maximum {max_minutes}min)"

    starts_local_date = starts_at.astimezone(tz).date()
    if starts_local_date != expected_local_date:
        return False, f"window starts on {starts_local_date}, expected {expected_local_date}"

    return True, ""


@dataclass
class RuleEvaluation:
    rule_id: str
    ok: bool
    reason: str
    service_type_id: str
    plan_id: Optional[str] = None
    plan_title: Optional[str] = None
    window_starts_at: Optional[datetime] = None
    window_ends_at: Optional[datetime] = None


def evaluate_rule(session: requests.Session, rule: Rule, today: date, tz: ZoneInfo) -> RuleEvaluation:
    """Decide whether `rule`'s service type has a plan today that's real
    enough to trust for automation.

    `ok=True` means the plan exists, has songs, and its plan_times window
    passed the sanity check -- the caller (admin_app.py's scheduler tick)
    still has to compare `now` against [window_starts_at, window_ends_at)
    to decide whether to actually act. Never raises: any PlanningCenterError
    (no plan today, a transient API error, ...) becomes ok=False with the
    error as `reason`, so one rule's failure can't block the others.
    """
    try:
        _, plan = find_plan_by_date(session, today, rule.service_type_id)
    except PlanningCenterError as exc:
        return RuleEvaluation(rule.id, False, f"no plan found for {today}: {exc}", rule.service_type_id)

    plan_id = plan["id"]
    plan_title = plan["attributes"].get("title") or plan["attributes"].get("series_title") or f"Plan {plan_id}"

    try:
        items = get_plan_items(session, rule.service_type_id, plan_id)
    except PlanningCenterError as exc:
        return RuleEvaluation(
            rule.id, False, f"could not load plan items: {exc}", rule.service_type_id, plan_id, plan_title
        )

    song_count = sum(1 for item in items if item.get("attributes", {}).get("item_type") == "song")
    if song_count == 0:
        return RuleEvaluation(rule.id, False, "plan has 0 songs", rule.service_type_id, plan_id, plan_title)

    try:
        plan_times = get_plan_times(session, rule.service_type_id, plan_id)
    except PlanningCenterError as exc:
        return RuleEvaluation(
            rule.id, False, f"could not load plan times: {exc}", rule.service_type_id, plan_id, plan_title
        )

    selected, reason = _select_service_plan_time(plan_times)
    if selected is None:
        return RuleEvaluation(rule.id, False, reason, rule.service_type_id, plan_id, plan_title)

    attrs = selected.get("attributes", {})
    try:
        starts_at = parse_pco_datetime(attrs["starts_at"])
        ends_at = parse_pco_datetime(attrs["ends_at"])
    except (KeyError, TypeError, ValueError) as exc:
        return RuleEvaluation(
            rule.id, False, f"malformed plan time: {exc}", rule.service_type_id, plan_id, plan_title
        )

    ok, reason = _is_sane_window(starts_at, ends_at, today, tz, rule.min_window_minutes, rule.max_window_minutes)
    return RuleEvaluation(
        rule.id,
        ok,
        reason if not ok else "ok",
        rule.service_type_id,
        plan_id,
        plan_title,
        starts_at,
        ends_at,
    )


# --------------------------------------------------------------------------
# Live-open-plan tracking (persisted at <DATA_DIR>/open_plan.json)
# --------------------------------------------------------------------------


@dataclass
class OpenPlan:
    service_type_id: str
    plan_id: str
    plan_title: str
    opened_by: Literal["manual", "automation"]
    rule_id: Optional[str] = None
    window_ends_at: Optional[str] = None  # ISO string; only meaningful when opened_by="automation"


def _open_plan_path(data_dir: Path) -> Path:
    return data_dir / "open_plan.json"


def read_open_plan(data_dir: Path) -> Optional[OpenPlan]:
    path = _open_plan_path(data_dir)
    if not path.exists():
        return None
    try:
        return OpenPlan(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        log.warning("Corrupt open_plan.json; treating as no plan tracked.")
        return None


def write_open_plan(data_dir: Path, open_plan: OpenPlan) -> None:
    path = _open_plan_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(asdict(open_plan), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def clear_open_plan(data_dir: Path) -> None:
    _open_plan_path(data_dir).unlink(missing_ok=True)
