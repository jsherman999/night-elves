"""The nightly run: drain the queue through plan -> execute -> review."""
from __future__ import annotations
import time
from datetime import datetime, timedelta
from pathlib import Path
from . import db, policy
from .backends import BackendError

PLAN_SYS = ("You are planning work that a smaller model will carry out. Do not do the task. "
            "Write a short numbered plan, the pitfalls to avoid, and how to tell the result is right.")
EXEC_SYS = "Complete the task fully and return only the finished result."
REVIEW_SYS = ("You are reviewing another model's work against the task. The first line of your "
              "reply must be exactly PASS or FAIL. Then give brief reasons, and for FAIL the fixes needed.")
AGENT_SYS = ("You are running unattended overnight and nobody can answer questions. The current "
             "directory is this task's workspace: do the work there with the tools you have, and "
             "save every file you create inside it, even if the task names another path. Finish "
             "with a short summary of what you did and the files you produced.")
RETRY_DELAY = timedelta(minutes=30)  # before a scheduled task is tried again after an error
DOWN_AFTER = 3  # consecutive errors before a backend is skipped for the rest of the run

def passed(review: str) -> bool:
    first = next((ln.strip().upper() for ln in review.splitlines() if ln.strip()), "")
    return not first.startswith("FAIL")

class Night:
    def __init__(self, cfg, conn, backends, now_fn=None, log=print):
        self.cfg, self.conn, self.backends, self.log = cfg, conn, backends, log
        self.now = now_fn or (lambda: datetime.now().astimezone())
        self.run_start = db.iso(self.now())
        # Nightly caps count from local midnight, so the scheduled-task runs during the day
        # share one allowance instead of each starting a fresh one.
        self.period_start = db.iso(self.now().replace(hour=0, minute=0, second=0, microsecond=0))
        self.errors: dict[str, int] = {}
        self.down: set[str] = set()
        self.held: set[int] = set()  # hit a backend error; wait for the next run
        self.stats = {"done": 0, "failed": 0, "retried": 0, "skipped": 0}

    def state(self):
        return policy.snapshot(self.cfg, self.conn, self.now(), self.period_start)

    def ok(self, name):
        return name in self.backends and policy.available(
            name, self.cfg, self.conn, self.state(), self.period_start, self.now(), self.down)

    def call(self, name, task_id, role, prompt, system, cwd=None):
        try:
            res = (self.backends[name].run(prompt, system, cwd=cwd) if cwd
                   else self.backends[name].run(prompt, system))
        except BackendError:
            self.errors[name] = self.errors.get(name, 0) + 1
            if self.errors[name] >= DOWN_AFTER:
                self.down.add(name)
                self.log(f"  {name} marked down for this run")
            raise
        self.errors[name] = 0
        db.record_spend(self.conn, name, task_id, role, res.tokens_in, res.tokens_out, res.usd,
                        ts=db.iso(self.now()))
        if not res.text.strip():
            raise BackendError(f"{name}: empty response")
        return res.text

    def process(self, task) -> str:
        """Returns the task's new status, or 'skipped' if nothing can take it tonight."""
        tid, lim = task["id"], self.cfg["limits"]
        r = policy.route(task, self.cfg, self.state(), self.ok)
        if r is None:
            return "skipped"
        self.log(f"#{tid} plan={r.planner} exec={r.executor} review={r.reviewer}")
        try:
            prompt = f"Task:\n{task['prompt']}"
            if r.planner:
                db.update(self.conn, tid, status="planning")
                plan = self.call(r.planner, tid, "plan", prompt, PLAN_SYS)
                prompt += f"\n\nPlan to follow:\n{plan}"
            if task["feedback"]:
                prompt += f"\n\nA previous attempt was rejected. Reviewer feedback:\n{task['feedback']}"
            db.update(self.conn, tid, status="running", backend=r.executor)
            work = None
            if task["tier"] == "agent":
                work = Path(self.cfg["paths"]["work"]) / str(tid)
                work.mkdir(parents=True, exist_ok=True)
            answer = self.call(r.executor, tid, "execute", prompt, AGENT_SYS if work else EXEC_SYS,
                               cwd=str(work) if work else None)
            review = ""
            if r.reviewer and self.ok(r.reviewer):
                db.update(self.conn, tid, status="review")
                review = self.call(r.reviewer, tid, "review",
                                   f"Task:\n{task['prompt']}\n\nResult:\n{answer}", REVIEW_SYS)
                if not passed(review):
                    return self._retry(task, feedback=review, error="rejected by reviewer")
        except BackendError as e:
            # One error attempt per run, so an outage can't use up every attempt in seconds.
            # Reviewer rejections above retry straight away, carrying the feedback.
            # Scheduled tasks are also picked up by the every-minute job, so push them back.
            self.held.add(tid)
            outcome = self._retry(task, error=str(e))
            if task["run_after"] and outcome == "retried":
                db.update(self.conn, tid, run_after=db.iso(self.now() + RETRY_DELAY))
            return outcome
        out = Path(self.cfg["paths"]["results"])
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{tid}.md"
        files = ""
        if work:
            names = sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file())
            files = (f"\n## Files in {work}\n\n" + "".join(f"- {n}\n" for n in names[:200])
                     if names else f"\n## Files\n\nNothing was saved in {work}.\n")
        path.write_text(f"# Task {tid}\n\n{task['prompt']}\n\n## Result ({r.executor})\n\n{answer}\n"
                        + files
                        + (f"\n## Review ({r.reviewer})\n\n{review}\n" if review else ""))
        db.update(self.conn, tid, status="done", result_path=str(path), last_error=None)
        return "done"

    def _retry(self, task, error, feedback=None) -> str:
        attempts = task["attempts"] + 1
        status = "failed" if attempts >= self.cfg["limits"]["max_attempts"] else "queued"
        fields = {"status": status, "attempts": attempts, "last_error": error[:1000]}
        if feedback:
            fields["feedback"] = feedback[:4000]
        db.update(self.conn, task["id"], **fields)
        self.log(f"  #{task['id']} -> {status}: {error[:120]}")
        return "failed" if status == "failed" else "retried"

    def run(self, max_tasks=None, due_only=False) -> dict:
        """due_only: just the scheduled tasks whose time has come (the every-minute job)."""
        stale = db.requeue_stale(self.conn)
        if stale:
            self.log(f"requeued {stale} task(s) left over from an interrupted run")
        deadline = time.monotonic() + self.cfg["limits"]["max_minutes"] * 60
        skipped: list[int] = []
        handled = 0
        while time.monotonic() < deadline and (max_tasks is None or handled < max_tasks):
            task = db.next_task(self.conn, [*skipped, *self.held], db.iso(self.now()), due_only)
            if task is None:
                break
            outcome = self.process(task)
            if outcome == "skipped":
                skipped.append(task["id"])
            else:
                handled += 1
            self.stats[outcome] += 1
        self.stats["run_start"] = self.run_start
        return self.stats
