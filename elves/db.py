"""SQLite queue and spend ledger."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'cli',        -- cli | web | sms | ...
  prompt TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5,       -- 1 = most important
  tier TEXT NOT NULL DEFAULT 'auto',         -- auto | local | frontier | agent
  status TEXT NOT NULL DEFAULT 'queued',     -- queued | planning | running | review | done | failed | cancelled
  parent_id INTEGER REFERENCES tasks(id),
  backend TEXT,                              -- backend that produced the result
  result_path TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  feedback TEXT,                             -- reviewer feedback carried into the next attempt
  last_error TEXT,
  run_after TEXT                             -- UTC; not started before this time
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority, id);
CREATE TABLE IF NOT EXISTS spend (
  ts TEXT NOT NULL,
  backend TEXT NOT NULL,
  task_id INTEGER,
  role TEXT,                                 -- plan | execute | review
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS spend_ts ON spend(backend, ts);
"""
TIERS = ("auto", "local", "frontier", "agent")
ACTIVE = ("planning", "running", "review")  # a run is working on it; hands off
EDITABLE = ("queued", "failed", "cancelled")

def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if "run_after" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute("ALTER TABLE tasks ADD COLUMN run_after TEXT")  # queues made before 0.2
    return conn

def add_task(conn, prompt: str, source="cli", priority=5, tier="auto", parent_id=None,
             run_after: str | None = None) -> int:
    prompt = prompt.strip()
    _check(prompt, priority, tier)
    now = iso()
    cur = conn.execute(
        "INSERT INTO tasks (created_at, updated_at, source, prompt, priority, tier, parent_id,"
        " run_after) VALUES (?,?,?,?,?,?,?,?)",
        (now, now, source, prompt, int(priority), tier, parent_id, run_after))
    conn.commit()
    return cur.lastrowid

def update(conn, task_id: int, **fields) -> None:
    fields["updated_at"] = iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    conn.commit()

def _check(prompt=None, priority=None, tier=None):
    if prompt is not None and not prompt.strip():
        raise ValueError("empty prompt")
    if priority is not None and not 1 <= int(priority) <= 9:
        raise ValueError("priority must be 1-9")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")

def change(conn, task_id: int, allowed=EDITABLE, **fields) -> None:
    """Edit, cancel or requeue a task, but only while it is in one of the allowed states, so a
    task a run has already started is never changed underneath it. Raises LookupError or
    ValueError with a message fit to show the user."""
    t = get(conn, task_id)
    if t is None:
        raise LookupError(f"no task #{task_id}")
    _check(fields.get("prompt"), fields.get("priority"), fields.get("tier"))
    if "prompt" in fields:
        fields["prompt"] = fields["prompt"].strip()
    if "priority" in fields:
        fields["priority"] = int(fields["priority"])
    fields["updated_at"] = iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    marks = ",".join("?" * len(allowed))
    cur = conn.execute(f"UPDATE tasks SET {sets} WHERE id=? AND status IN ({marks})",
                       (*fields.values(), task_id, *allowed))
    conn.commit()
    if not cur.rowcount:
        status = get(conn, task_id)["status"]
        verb = "running" if status in ACTIVE else status
        raise ValueError(f"#{task_id} is {verb}, so it can't be changed now")

def cancel(conn, task_id: int) -> None:
    change(conn, task_id, ("queued", "failed"), status="cancelled")

def requeue(conn, task_id: int) -> None:
    """Back into the queue with fresh attempts: for failed, cancelled or finished tasks."""
    change(conn, task_id, ("failed", "cancelled", "done"), status="queued", attempts=0,
           last_error=None)

def claim(conn, task_id: int, status: str) -> bool:
    """Start work on a task, unless it was cancelled or edited away since it was picked."""
    cur = conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status='queued'",
                       (status, iso(), task_id))
    conn.commit()
    return cur.rowcount == 1

def get(conn, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

def next_task(conn, skip=(), now: str | None = None, due_only=False):
    """Scheduled tasks that are due come first. due_only leaves out unscheduled tasks."""
    marks = ",".join("?" * len(skip))
    where = f"AND id NOT IN ({marks})" if skip else ""
    if due_only:
        where += " AND run_after IS NOT NULL"
    return conn.execute(
        f"SELECT * FROM tasks WHERE status='queued' AND (run_after IS NULL OR run_after<=?) {where}"
        " ORDER BY run_after IS NULL, priority, id LIMIT 1",
        (now or iso(), *skip)).fetchone()

def list_tasks(conn, status=None, limit=50):
    if status:
        return conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, limit)).fetchall()
    return conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def requeue_stale(conn) -> int:
    """Tasks left mid-flight by a crashed run go back to the queue."""
    cur = conn.execute("UPDATE tasks SET status='queued', updated_at=? "
                       "WHERE status IN ('planning','running','review')", (iso(),))
    conn.commit()
    return cur.rowcount

def record_spend(conn, backend, task_id, role, tokens_in=0, tokens_out=0, usd=0.0, ts=None):
    conn.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?)",
                 (ts or iso(), backend, task_id, role, tokens_in, tokens_out, usd))
    conn.commit()

def usd_since(conn, backend: str, since: str) -> float:
    return conn.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE backend=? AND ts>=?",
                        (backend, since)).fetchone()[0]

def runs_since(conn, backend: str, since: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM spend WHERE backend=? AND ts>=?",
                        (backend, since)).fetchone()[0]
