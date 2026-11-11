"""Decide which backend plans, executes, and reviews each task.

The monthly API credit expires unused, so the policy is: stay local early in the
cycle, then spend what is left at an even pace, and in the last days let the
frontier model do the hard tasks outright.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
from . import db

MIN_USD = 0.05  # below this, treat tonight's API allowance as used up

@dataclass
class State:
    mode: str            # conserve | pace | endgame
    cycle_start: str     # UTC ISO timestamp
    days_left: int
    elapsed: float       # share of the cycle that has passed, 0..1
    credit: float
    spent: float
    tonight_allowance: float
    tonight_spent: float

    @property
    def remaining(self) -> float:
        return max(self.credit - self.spent, 0.0)

    @property
    def tonight_left(self) -> float:
        return max(min(self.tonight_allowance - self.tonight_spent, self.remaining), 0.0)

@dataclass
class Route:
    planner: str | None
    executor: str
    reviewer: str | None

def cycle_bounds(today: date, cycle_day: int) -> tuple[date, date]:
    y, m = today.year, today.month
    if today.day < cycle_day:
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    start = date(y, m, cycle_day)
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    return start, date(ny, nm, cycle_day)

def snapshot(cfg: dict, conn, now: datetime, run_start: str) -> State:
    b = cfg["budget"]
    api = cfg["roles"]["frontier_api"]
    start, end = cycle_bounds(now.date(), b["cycle_day"])
    start_iso = db.iso(datetime(start.year, start.month, start.day, tzinfo=now.tzinfo))
    days_left = max((end - now.date()).days, 1)
    elapsed = (now.date() - start).days / (end - start).days
    spent = db.usd_since(conn, api, start_iso)
    remaining = max(b["monthly_credit_usd"] - spent, 0.0)
    if elapsed < b["start_fraction"]:
        mode, allowance = "conserve", b["conserve_night_cap_usd"]
    else:
        mode = "endgame" if days_left <= b["endgame_days"] else "pace"
        allowance = min(remaining / days_left * b["burst"], b["per_night_cap_usd"])
    return State(mode, start_iso, days_left, elapsed, b["monthly_credit_usd"], spent,
                 allowance, db.usd_since(conn, api, run_start))

def in_window(window, hour: int) -> bool:
    if not window:
        return True
    a, z = window
    return a <= hour < z if a <= z else (hour >= a or hour < z)

def available(name, cfg, conn, state: State, run_start: str, now: datetime, down=()) -> bool:
    c = cfg["backends"].get(name)
    if not c or not c.get("enabled", True) or name in down:
        return False
    if c["type"] == "anthropic_api":
        return state.tonight_left > MIN_USD
    if c["type"] == "command":
        return (in_window(c.get("window"), now.hour)
                and db.runs_since(conn, name, run_start) < c.get("max_runs_per_night", 5))
    return True

def route(task, cfg: dict, state: State, ok) -> Route | None:
    """ok(name) says whether a backend can be used right now. None = leave queued."""
    r, lim = cfg["roles"], cfg["limits"]
    local = r["local"] if ok(r["local"]) else None
    frontier = next((n for n in (r["frontier_api"], r["frontier_sub"]) if ok(n)), None)
    second = r["second_opinion"] if ok(r["second_opinion"]) else None

    if task["tier"] == "local":
        return Route(None, local, None) if local else None
    if task["tier"] == "frontier":
        return Route(None, frontier, None) if frontier else None
    if task["attempts"] >= lim["escalate_after"] and frontier:
        return Route(None, frontier, None)  # local kept failing: escalate
    if state.mode == "endgame" and frontier and task["priority"] <= lim["hard_priority"]:
        return Route(None, frontier, None)
    if not local:
        return Route(None, frontier, None) if frontier else None
    if state.mode == "conserve":
        return Route(None, local, second)
    return Route(frontier, local, frontier or second)
