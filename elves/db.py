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
  status TEXT NOT NULL DEFAULT 'queued',     -- queued | planning | running | review | done | failed
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
    if not prompt:
        raise ValueError("empty prompt")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
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
