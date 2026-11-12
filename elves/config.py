"""Load config.toml and fill in defaults."""
from __future__ import annotations
import copy, os, tomllib
from pathlib import Path

DEFAULTS = {
    "paths": {"db": "~/.night-elves/queue.db", "results": "~/.night-elves/results",
              "work": "~/.night-elves/work",   # agent-tier tasks each get work/<id>/
              "attachments": "~/.night-elves/attachments"},  # a task's files: attachments/<id>/
    "budget": {
        "monthly_credit_usd": 100.0,   # API credit that expires each cycle
        "cycle_day": 1,                # day of month the credit refreshes (1-28)
        "start_fraction": 0.5,         # start spending credit after this share of the cycle
        "endgame_days": 7,             # last N days: frontier model also executes hard tasks
        "burst": 1.5,                  # tonight's allowance = remaining/days_left * burst
        "per_night_cap_usd": 15.0,
        "conserve_night_cap_usd": 2.0, # early-cycle cap, for escalations only
    },
    "limits": {"max_attempts": 3, "escalate_after": 2, "max_minutes": 360, "hard_priority": 3},
    "roles": {"local": "local", "frontier_api": "claude_api",
              "frontier_sub": "claude_cli", "second_opinion": "codex", "agent": "claude_agent",
              "local_vision": ""},  # a local model that sees images, e.g. "local_vision"
    "backends": {},
    "web": {"host": "127.0.0.1", "port": 8377, "token_env": "ELVES_TOKEN", "sms_allow": [],
            "max_upload_mb": 2048,  # one task's files, sent through the web intake
            "public_url": ""},  # e.g. the Tailscale address; digests link each result there
    "notify": {"argv": []},
}

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

def find_config(path: str | None = None) -> Path | None:
    for c in [path, os.environ.get("ELVES_CONFIG"), "config.toml", "~/.night-elves/config.toml"]:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser()
    return None

def load(path: str | None = None) -> dict:
    p = find_config(path)
    cfg = _merge(DEFAULTS, tomllib.loads(p.read_text()) if p else {})
    for k in ("db", "results", "work", "attachments"):
        cfg["paths"][k] = str(Path(cfg["paths"][k]).expanduser())
    day = cfg["budget"]["cycle_day"]
    if not 1 <= day <= 28:
        raise ValueError("budget.cycle_day must be between 1 and 28")
    return cfg
