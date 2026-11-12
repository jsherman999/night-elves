"""Morning digest: what finished, what failed, what was spent."""
from __future__ import annotations
import subprocess, sys
from datetime import datetime
from pathlib import Path
from . import policy

def digest(cfg, conn, since: str, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    st = policy.snapshot(cfg, conn, now, since)
    rows = conn.execute("SELECT id, status, backend, prompt, last_error FROM tasks "
                        "WHERE updated_at>=? ORDER BY id", (since,)).fetchall()
    queued = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
    lines = [f"night-elves {now:%a %b %d}: "
             f"{sum(r['status'] == 'done' for r in rows)} done, "
             f"{sum(r['status'] == 'failed' for r in rows)} failed, {queued} still queued"]
    for r in rows:
        if r["status"] in ("done", "failed"):
            title = " ".join(r["prompt"].split())[:60]
            tail = f" ({r['last_error'][:60]})" if r["status"] == "failed" and r["last_error"] else ""
            lines.append(f"  #{r['id']} {r['status']} [{r['backend'] or '-'}] {title}{tail}")
    lines.append(f"API credit: ${st.tonight_spent:.2f} tonight, ${st.spent:.2f} of "
                 f"${st.credit:.0f} this cycle, {st.days_left}d left ({st.mode})")
    return "\n".join(lines)

def send(cfg, text: str, name: str | None = None) -> None:
    out = Path(cfg["paths"]["results"])
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name or f'digest-{datetime.now():%Y-%m-%d}'}.txt").write_text(text + "\n")
    argv = cfg["notify"].get("argv") or []
    if not argv:
        return
    as_arg = any("{text}" in a for a in argv)
    try:
        p = subprocess.run([a.replace("{text}", text) for a in argv], input=None if as_arg else text,
                           text=True, timeout=cfg["notify"].get("timeout", 60), check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"notify failed: {e}", file=sys.stderr)  # the digest file is already written
        return
    if p.returncode != 0:
        print(f"notify failed: {argv[0]} exited {p.returncode}", file=sys.stderr)
