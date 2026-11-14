"""SQLite queue and spend ledger."""
from __future__ import annotations
import json, re, sqlite3
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
  run_after TEXT,                            -- UTC; not started before this time
  images TEXT,                               -- 0.4.0 only; moved to files
  files TEXT,                                -- JSON [{name, kind, size}] in attachments/<id>/
  criteria TEXT,                             -- "done when": what the result must satisfy
  session TEXT,                              -- "<backend> <id>" of the agent session to resume
  needs TEXT,                                -- auto tier: agent | web | answer, decided once
  secs REAL,                                 -- wall-clock seconds spent on it, all attempts
  archived INTEGER NOT NULL DEFAULT 0        -- 1 = off the recent list; the result is kept
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority, id);
CREATE TABLE IF NOT EXISTS spend (
  ts TEXT NOT NULL,
  backend TEXT NOT NULL,
  task_id INTEGER,
  role TEXT,                                 -- plan | execute | review
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  usd REAL NOT NULL DEFAULT 0,               -- billed against the API credit
  est_usd REAL,                              -- the same work at API prices (NULL = usd)
  secs REAL                                  -- how long the call took
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, kind in (("run_after", "TEXT"), ("images", "TEXT"), ("files", "TEXT"),
                      ("criteria", "TEXT"), ("session", "TEXT"),  # before 0.2/0.4/0.5/0.7
                      ("needs", "TEXT"), ("secs", "REAL"),  # before 0.9
                      ("archived", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {kind}")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(spend)")}
    for col in ("est_usd", "secs"):
        if col not in scols:
            conn.execute(f"ALTER TABLE spend ADD COLUMN {col} REAL")
    old = conn.execute("SELECT id, images FROM tasks WHERE images IS NOT NULL AND files IS NULL")
    for tid, images in old.fetchall():  # 0.4.0 kept a list of image names
        conn.execute("UPDATE tasks SET files=? WHERE id=?", (json.dumps(
            [{"name": n, "kind": "image", "size": None} for n in json.loads(images)]), tid))
    conn.commit()
    return conn

def add_task(conn, prompt: str, source="cli", priority=5, tier="auto", parent_id=None,
             run_after: str | None = None, files=(), on_insert=None, vision=False,
             criteria: str | None = None) -> int:
    """on_insert(id) runs before the commit (attachments use it to move their files into
    place), so other connections never see the task without them. If it raises, nothing is
    added."""
    prompt = prompt.strip()
    _check(prompt, priority, tier, list(files), vision)
    now = iso()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (created_at, updated_at, source, prompt, priority, tier, parent_id,"
            " run_after, files, criteria) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, now, source, prompt, int(priority), tier, parent_id, run_after,
             json.dumps(list(files)) if files else None, (criteria or "").strip() or None))
        if on_insert:
            on_insert(cur.lastrowid)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return cur.lastrowid

def update(conn, task_id: int, **fields) -> None:
    fields["updated_at"] = iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    conn.commit()

def _check(prompt=None, priority=None, tier=None, files=(), vision=False):
    """vision: a local vision model is configured, so the local tier can take images."""
    if prompt is not None and not prompt.strip():
        raise ValueError("empty prompt")
    if priority is not None and not 1 <= int(priority) <= 9:
        raise ValueError("priority must be 1-9")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    if tier is not None and files:
        from .attachments import check_tier
        check_tier(tier, list(files), vision)

def change(conn, task_id: int, allowed=EDITABLE, vision=False, **fields) -> None:
    """Edit, cancel or requeue a task, but only while it is in one of the allowed states, so a
    task a run has already started is never changed underneath it. Raises LookupError or
    ValueError with a message fit to show the user."""
    t = get(conn, task_id)
    if t is None:
        raise LookupError(f"no task #{task_id}")
    _check(fields.get("prompt"), fields.get("priority"), fields.get("tier"),
           json.loads(t["files"] or "[]"), vision)
    if "prompt" in fields:
        fields["prompt"] = fields["prompt"].strip()
    if "priority" in fields:
        fields["priority"] = int(fields["priority"])
    if "criteria" in fields:
        fields["criteria"] = (fields["criteria"] or "").strip() or None
    if "prompt" in fields or "tier" in fields:
        fields["needs"] = None  # an edited auto task is sized up again
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
           last_error=None, archived=0)

def archive(conn, task_id: int, on: bool = True) -> None:
    """Take a finished, failed or cancelled task off the recent list (or put it back). Its row,
    result file and workspace stay, so it can still be searched for and referenced."""
    t = get(conn, task_id)
    if t is None:
        raise LookupError(f"no task #{task_id}")
    if on and t["status"] not in ("done", "failed", "cancelled"):
        raise ValueError(f"#{task_id} hasn't finished, so it can't be archived")
    conn.execute("UPDATE tasks SET archived=? WHERE id=?", (int(on), task_id))  # not updated_at:
    conn.commit()                                                 # that is when it finished

MAX_SEARCH = 2 * 2**20  # bytes of each result file that a search reads

def search(conn, query: str, results: str | Path | None = None, limit: int = 50) -> list:
    """Tasks whose prompt or result contains every word of the query, any case, newest first,
    archived ones included. Returns [(task, snippet)]: the snippet is the result text around
    the first word found there, or "" when only the prompt matched. With `results`, only
    result files inside that folder are read."""
    words = query.lower().split()
    if not words:
        return []
    root = Path(results).expanduser().resolve() if results else None
    out = []
    for t in conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall():
        body = ""
        if t["result_path"]:
            p = Path(t["result_path"]).resolve()
            if (root is None or p.is_relative_to(root)) and p.is_file():
                with open(p, errors="replace") as f:
                    body = f.read(MAX_SEARCH)
        hay = (t["prompt"] + "\n" + body).lower()
        if not all(w in hay for w in words):
            continue
        hits = [m for w in words if (m := re.search(re.escape(w), body, re.I))]
        snippet = ""
        if hits:
            m = min(hits, key=lambda m: m.start())
            a, b = max(0, m.start() - 60), min(len(body), m.end() + 100)
            snippet = (("…" if a else "") + " ".join(body[a:b].split())
                       + ("…" if b < len(body) else ""))
        out.append((t, snippet))
        if len(out) >= limit:
            break
    return out

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

def list_tasks(conn, status=None, limit=50, archived=None):
    """archived: True for only archived tasks, False for only the others, None for both."""
    if archived is not None:
        return conn.execute("SELECT * FROM tasks WHERE archived=? ORDER BY id DESC LIMIT ?",
                            (int(archived), limit)).fetchall()
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

def record_spend(conn, backend, task_id, role, tokens_in=0, tokens_out=0, usd=0.0, ts=None,
                 est_usd=None, secs=None):
    conn.execute("INSERT INTO spend (ts, backend, task_id, role, tokens_in, tokens_out, usd,"
                 " est_usd, secs) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ts or iso(), backend, task_id, role, tokens_in, tokens_out, usd, est_usd, secs))
    conn.commit()

def add_secs(conn, task_id: int, secs: float) -> None:
    conn.execute("UPDATE tasks SET secs=COALESCE(secs,0)+? WHERE id=?", (secs, task_id))
    conn.commit()

def usage(conn, task_ids) -> dict:
    """Per task: {"in", "out", "usd" (billed), "est" (at API prices), "calls", "by": {backend:
    {"in", "out", "est"}}}, from the spend ledger."""
    ids = list(task_ids)
    out: dict = {}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in conn.execute(
            "SELECT task_id, backend, SUM(tokens_in), SUM(tokens_out), SUM(usd), "
            f"SUM(COALESCE(est_usd, usd)), COUNT(*) FROM spend WHERE task_id IN ({marks}) "
            "GROUP BY task_id, backend ORDER BY MIN(ts)", ids):
        u = out.setdefault(r[0], {"in": 0, "out": 0, "usd": 0.0, "est": 0.0, "calls": 0, "by": {}})
        u["in"] += r[2] or 0
        u["out"] += r[3] or 0
        u["usd"] += r[4] or 0.0
        u["est"] += r[5] or 0.0
        u["calls"] += r[6]
        u["by"][r[1]] = {"in": r[2] or 0, "out": r[3] or 0, "est": r[5] or 0.0}
    return out

def usd_since(conn, backend: str, since: str) -> float:
    return conn.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE backend=? AND ts>=?",
                        (backend, since)).fetchone()[0]

def runs_since(conn, backend: str, since: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM spend WHERE backend=? AND ts>=?",
                        (backend, since)).fetchone()[0]
