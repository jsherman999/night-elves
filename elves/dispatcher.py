"""The nightly run: drain the queue through plan -> execute -> review."""
from __future__ import annotations
import subprocess, time
from datetime import datetime, timedelta
from pathlib import Path
from . import attachments, db, policy
from .backends import BackendError

PLAN_SYS = ("You are planning work that a smaller model will carry out. Do not do the task. "
            "Write a short numbered plan, the pitfalls to avoid, and how to tell the result is right.")
EXEC_SYS = "Complete the task fully and return only the finished result."
REVIEW_SYS = ("You are reviewing another model's work against the task. The first line of your "
              "reply must be exactly PASS or FAIL. Then give brief reasons, and for FAIL the fixes needed.")
AGENT_SYS = ("You are running unattended overnight and nobody can answer questions, so make "
             "reasonable choices and carry on. The current directory is this task's workspace: do "
             "the work there with the tools you have, and save every file you create inside it, "
             "even if the task names another path. Commands run in a sandbox: only the workspace "
             "is writable, and the network is limited to package registries and a few sites. "
             "Tasks come from this Mac's owner. If home-network addresses are allowed, they "
             "can be reached over TCP only through the sandbox proxy ($HTTPS_PROXY, HTTP "
             "CONNECT): the private ranges are in NO_PROXY, so override that (`curl --noproxy "
             "''`) and have scripts connect through the proxy. Ping, UDP, mDNS and packet "
             "capture are blocked; `arp -an` works. "
             "When you build software, keep dependencies local (a .venv or node_modules), run "
             "the build and tests and fix what fails, stop any server you start (by its PID), "
             "and write a README.md saying how to run it. Don't run `git init` (the sandbox "
             "blocks it, so use `cargo new --vcs none`); the workspace is committed to git for "
             "you when you finish. Finish with a short summary of what you did, what you "
             "verified, and anything left undone.")
SKIP_LISTING = {"node_modules", "target", "__pycache__", "venv"}  # plus anything hidden
GIT_EXCLUDE = ".cache/\n.claude/\nnode_modules/\ntarget/\n.venv/\nvenv/\n__pycache__/\n.DS_Store\n"

def commit_workspace(work: Path, message: str) -> str:
    """Put an agent's workspace under git, outside the sandbox. Returns a line for the result."""
    def git(*args):
        return subprocess.run(["git", "-C", str(work), "-c", "user.name=night-elves",
                               "-c", "user.email=night-elves@localhost", *args],
                              capture_output=True, text=True, timeout=120, check=True)
    try:
        if not (work / ".git").exists():
            git("init", "-q")
            (work / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (work / ".git" / "info" / "exclude").write_text(GIT_EXCLUDE)
        git("add", "-A")
        if git("status", "--porcelain").stdout.strip():
            git("commit", "-q", "-m", message)
        return f"Committed to git in {work} ({git('rev-parse', '--short', 'HEAD').stdout.strip()})."
    except (OSError, subprocess.SubprocessError) as e:
        return f"Could not commit the workspace to git: {e}"
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

    def call(self, name, task_id, role, prompt, system, cwd=None, files=()):
        kw = {k: v for k, v in (("cwd", cwd), ("files", files)) if v}
        try:
            res = self.backends[name].run(prompt, system, **kw)
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
        if not db.claim(self.conn, tid, "planning" if r.planner else "running"):
            return "skipped"  # cancelled or changed since it was picked
        try:
            work, cwd, send = None, None, []
            if task["tier"] == "agent":
                work = Path(self.cfg["paths"]["work"]) / str(tid)
                work.mkdir(parents=True, exist_ok=True)
                cwd = work
            prompt = f"Task:\n{task['prompt']}"
            if attachments.files(task):
                prompt, cwd, send = self._attach(task, r.executor, prompt, work, cwd)
            if r.planner:
                plan = self.call(r.planner, tid, "plan", prompt, PLAN_SYS)
                prompt += f"\n\nPlan to follow:\n{plan}"
            if task["feedback"]:
                prompt += f"\n\nA previous attempt was rejected. Reviewer feedback:\n{task['feedback']}"
            db.update(self.conn, tid, status="running", backend=r.executor)
            answer = self.call(r.executor, tid, "execute", prompt, AGENT_SYS if work else EXEC_SYS,
                               cwd=str(cwd) if cwd else None, files=send)
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
            rels = (p.relative_to(work) for p in work.rglob("*") if p.is_file())
            names = sorted(str(r) for r in rels
                           if not any(s.startswith(".") or s in SKIP_LISTING for s in r.parts))
            files = (f"\n## Files in {work}\n\n" + "".join(f"- {n}\n" for n in names[:200])
                     if names else f"\n## Files\n\nNothing was saved in {work}.\n")
            if names:
                title = " ".join(task["prompt"].split())[:60]
                files += "\n" + commit_workspace(work, f"Task {tid}: {title}") + "\n"
        attached = ""
        if attachments.files(task):
            attached = (f"Attached, in {attachments.task_dir(self.cfg, tid)}:"
                        + attachments.listing(task) + "\n\n")
        path.write_text(f"# Task {tid}\n\n{task['prompt']}\n\n{attached or ''}"
                        f"## Result ({r.executor})\n\n{answer}\n"
                        + files
                        + (f"\n## Review ({r.reviewer})\n\n{review}\n" if review else ""))
        db.update(self.conn, tid, status="done", result_path=str(path), last_error=None)
        return "done"

    def _attach(self, task, executor, prompt, work, cwd):
        """Give the executor the task's files. Returns (prompt, cwd, files to send)."""
        if missing := [p.name for p in attachments.paths(self.cfg, task) if not p.is_file()]:
            raise BackendError(f"attached file(s) missing: {', '.join(missing)}")
        if work:  # the agent's file tools only reach its workspace
            attachments.copy_into(self.cfg, task, work / "input")
            return (prompt + "\n\nFiles attached to this task, copied into ./input/ (work on "
                    "them with your tools; read big ones in parts or with code, not all at once):"
                    + attachments.listing(task, "input/"), cwd, [])
        prompt += attachments.inline_text(self.cfg, task)
        seen = attachments.paths(self.cfg, task, attachments.CLAUDE_ONLY)
        if seen and self.cfg["backends"].get(executor, {}).get("type") == "command":
            return (prompt + "\n\nImages and PDFs attached to this task (open each one with the "
                    "Read tool before you start):" + "".join(f"\n- {p}" for p in seen),
                    attachments.task_dir(self.cfg, task["id"]), [])  # claude -p reads in its cwd
        return prompt, cwd, seen

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
