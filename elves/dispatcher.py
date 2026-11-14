"""The nightly run: drain the queue through plan -> execute -> review."""
from __future__ import annotations
import re, subprocess, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from . import attachments, backends, db, policy, push, verify
from .backends import BackendError, SpentError

PLAN_SYS = ("You are planning work that a smaller model will carry out. Do not do the task. "
            "Write a short numbered plan and the pitfalls to avoid. End with a section that starts "
            "with the line 'Done when:' and lists the conditions a reviewer can check the result "
            "against.")
EXEC_SYS = "Complete the task fully and return only the finished result."
REVIEW_SYS = ("You are reviewing another model's work against the task. The first line of your "
              "reply must be exactly PASS or FAIL. Check the result against the task and, when "
              "they are given, every 'Done when' condition: FAIL if any of them is not met. Then "
              "give brief reasons, and for FAIL the fixes needed.")
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
             "Before deciding something can't be done, look for the tool: `ls /opt/homebrew/bin` "
             "lists what is installed. Nothing can be installed system-wide. macOS won't start a "
             "sandbox inside this one, so build Swift packages with `swift build "
             "--disable-sandbox` (and `swift test --disable-sandbox`). "
             "When you build software, keep dependencies local (a .venv or node_modules), run "
             "the build and tests and fix what fails, stop any server you start (by its PID), "
             "and write a README.md saying how to run it. Don't run `git init` (the sandbox "
             "blocks it, so use `cargo new --vcs none`); the workspace is committed to git for "
             "you when you finish. Finish with a short summary of what you did, what you "
             "verified, and anything left undone.")
CONTRACT = ("End your reply with these two lines:\n"
            "STATUS: DONE   (or STATUS: INCOMPLETE if anything asked for isn't done or doesn't work)\n"
            "CHECK: <one shell command>\n"
            "When you have finished, night-elves runs the CHECK command itself, from the workspace, "
            "in a sandbox with no network except localhost, for at most {minutes} minutes, and "
            "accepts the work only if it exits 0. Make it prove the task is done: run the tests, or "
            "a script that checks what you produced. Put anything longer than one line in a script "
            "(check.sh) and run that. It must not install anything, and it must start and stop any "
            "server it needs. Write CHECK: none only when no command could test the result.")
CLASSIFY_SYS = (
    "Sort a task by what it needs to be done well. Reply with exactly one of these words: "
    "AGENT, WEB or ANSWER.\n"
    "AGENT: doing it takes tools. It asks to clone, download, run, build, test, install, fix, "
    "change, add to, port or deploy code, an app or a repository; to create or edit files, "
    "sites, images, audio or video; to commit or push; or to work through big data files. "
    "Answer AGENT whenever the task asks for something to be made or changed, even if it also "
    "needs the web.\n"
    "WEB: only a written answer is wanted, but it needs current or specific information from "
    "the internet: searching, reviews, news, prices, or reading web pages or a repository.\n"
    "ANSWER: a written answer from general knowledge and the task's own text or attachments.\n"
    "Examples: 'clone this repo and find bugs' AGENT; 'add a GitHub Pages version of this app' "
    "AGENT; 'make a video about this app' AGENT; 'find reviews of this product' WEB; 'what "
    "does this GitHub repo do' WEB; 'explain how TCP works' ANSWER.")
NEEDS = re.compile(r"\b(AGENT|TOOLS?|WEB|SEARCH|ANSWER)\b")
NEEDS_MAP = {"TOOL": "agent", "TOOLS": "agent", "SEARCH": "web"}
CONTINUE = ("This continues your earlier work on the task below, in the same workspace; your "
            "files from before are still there.")
NOT_ACCEPTED = "not accepted: "  # starts last_error when feedback holds the reason
SKIP_LISTING = {"node_modules", "target", "__pycache__", "venv"}  # plus anything hidden
GIT_EXCLUDE = ".cache/\n.claude/\nnode_modules/\ntarget/\n.venv/\nvenv/\n__pycache__/\n.DS_Store\n"

def commit_workspace(work: Path, message: str) -> str:
    """Put an agent's workspace under git, outside the sandbox. Returns a line for the result."""
    def git(*args):
        return subprocess.run(["git", "-C", str(work), "-c", "user.name=night-elves",
                               "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
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

VERDICT_LINE = re.compile(r"^[\W_]*(?:verdict|result|review)?[\W_]*(PASS|FAIL)", re.I)
VERDICT_WORD = re.compile(r"\b(PASS|FAIL)(?:ED|ES|S)?\b", re.I)
DONE_WHEN = re.compile(r"^[#*_\s]*done when[*_\s]*:?[*_]*[ \t]*", re.I | re.M)

def passed(review: str) -> bool:
    """The reviewer's verdict: PASS or FAIL on the first line, even dressed up as **FAIL** or
    "Verdict: FAIL"; failing that, the first PASS or FAIL in the reply. No verdict at all
    counts as a pass, so a reviewer that ignores the format can't fail good work."""
    first = next((ln for ln in review.splitlines() if ln.strip()), "")
    m = VERDICT_LINE.match(first) or VERDICT_WORD.search(review)
    return not (m and m.group(1).upper() == "FAIL")

def plan_criteria(plan: str) -> str:
    """The 'Done when:' section a planner wrote, if it wrote one."""
    m = DONE_WHEN.search(plan)
    return plan[m.end():].strip() if m else ""

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

    def call(self, name, task_id, role, prompt, system, cwd=None, files=(), args=(), web=False):
        kw = {k: v for k, v in (("cwd", cwd), ("files", files), ("args", args), ("web", web)) if v}
        t0 = time.monotonic()
        def spent(res):
            db.record_spend(self.conn, name, task_id, role, res.tokens_in, res.tokens_out,
                            res.usd, ts=db.iso(self.now()), est_usd=res.est_usd,
                            secs=round(time.monotonic() - t0, 1))
        try:
            res = self.backends[name].run(prompt, system, **kw)
        except BackendError as e:
            if isinstance(e, SpentError):  # failed, but the tokens were used (and billed)
                spent(e.result)
            self.errors[name] = self.errors.get(name, 0) + 1
            if self.errors[name] >= DOWN_AFTER:
                self.down.add(name)
                self.log(f"  {name} marked down for this run")
            raise
        self.errors[name] = 0
        spent(res)
        if not res.text.strip():
            raise BackendError(f"{name}: empty response")
        return res.text

    def size_up(self, task):
        """An auto task is sorted once, by a cheap local call: one that needs tools becomes an
        agent task, one that needs the web goes to Claude with web search. Returns the task."""
        if task["tier"] != "auto" or task["needs"]:
            return task
        name = self.cfg["roles"].get("classifier") or self.cfg["roles"]["local"]
        if name == "off" or not self.ok(name):
            return task
        try:
            reply = self.call(name, task["id"], "classify", f"Task:\n{task['prompt']}",
                              CLASSIFY_SYS)
        except BackendError as e:
            self.log(f"  #{task['id']}: couldn't size it up ({e}); treating it as an answer")
            return task
        m = NEEDS.search(reply.upper())
        needs = NEEDS_MAP.get(m.group(1), m.group(1).lower()) if m else "answer"
        fields = {"needs": needs}
        if needs == "agent":
            fields["tier"] = "agent"
        db.update(self.conn, task["id"], **fields)
        self.log(f"  #{task['id']} needs: {needs}")
        return db.get(self.conn, task["id"])

    def process(self, task) -> str:
        """Returns the task's new status, or 'skipped' if nothing can take it tonight."""
        task = self.size_up(task)
        tid = task["id"]
        r = policy.route(task, self.cfg, self.state(), self.ok)
        if r is None:
            return "skipped"
        self.log(f"#{tid} plan={r.planner} exec={r.executor} review={r.reviewer}")
        if not db.claim(self.conn, tid, "planning" if r.planner else "running"):
            return "skipped"  # cancelled or changed since it was picked
        t0 = time.monotonic()
        try:
            return self._process(task, r)
        finally:
            db.add_secs(self.conn, tid, round(time.monotonic() - t0, 1))

    def _process(self, task, r) -> str:
        tid = task["id"]
        try:
            work, cwd, send = None, None, []
            if task["tier"] == "agent":
                work = Path(self.cfg["paths"]["work"]) / str(tid)
                work.mkdir(parents=True, exist_ok=True)
                cwd = work
            crit = (task["criteria"] or "").strip()
            prompt = f"Task:\n{task['prompt']}"
            if crit:
                prompt += f"\n\nThe task is done only when:\n{crit}"
            if attachments.files(task):
                prompt, cwd, send = self._attach(task, r.executor, prompt, work, cwd)
            if r.planner:
                plan = self.call(r.planner, tid, "plan", prompt, PLAN_SYS)
                prompt += f"\n\nPlan to follow:\n{plan}"
                crit = crit or plan_criteria(plan)
            db.update(self.conn, tid, status="running", backend=r.executor)
            if work:
                answer = self._agent(task, r.executor, prompt, cwd)
                ok, check, feedback = self._verify(r.executor, work, answer)
                if not ok:
                    self._write_result(task, r.executor, answer, work, check=check,
                                       accepted=False)
                    return self._retry(task, feedback=feedback,
                                       error=NOT_ACCEPTED + feedback.split("\n")[0][:200])
                env = backends.environ(self.cfg["backends"].get(r.executor, {}), str(work))
                pushed = push.run(self.cfg, work, task["prompt"], answer, env)
                if pushed:
                    self.log(f"  #{tid} push: {pushed.splitlines()[0][:120]}")
                self._write_result(task, r.executor, answer, work, check=check, pushed=pushed)
                return self._done(task)
            if task["feedback"]:
                prompt += f"\n\nA previous attempt was rejected. Reviewer feedback:\n{task['feedback']}"
            answer = self.call(r.executor, tid, "execute", prompt, EXEC_SYS,
                               cwd=str(cwd) if cwd else None, files=send,
                               web=policy.local_search(self.cfg, r.executor))
            review = ""
            if r.reviewer and self.ok(r.reviewer):
                db.update(self.conn, tid, status="review")
                done_when = f"\n\nDone when:\n{crit}" if crit else ""
                review = self.call(r.reviewer, tid, "review",
                                   f"Task:\n{task['prompt']}{done_when}\n\nResult:\n{answer}",
                                   REVIEW_SYS)
                if not passed(review):
                    return self._retry(task, feedback=review,
                                       error=NOT_ACCEPTED + "rejected by the reviewer")
        except BackendError as e:
            # One error attempt per run, so an outage can't use up every attempt in seconds.
            # Rejections (reviewer, agent check) retry straight away, carrying the feedback.
            # Scheduled tasks are also picked up by the every-minute job, so push them back.
            self.held.add(tid)
            outcome = self._retry(task, error=str(e))
            if task["run_after"] and outcome == "retried":
                db.update(self.conn, tid, run_after=db.iso(self.now() + RETRY_DELAY))
            return outcome
        self._write_result(task, r.executor, answer, None, review=review, reviewer=r.reviewer)
        return self._done(task)

    def _done(self, task) -> str:
        db.update(self.conn, task["id"], status="done", last_error=None, feedback=None)
        return "done"

    def _why(self, task) -> str:
        """What happened to the attempts before this one, for an agent that tries again."""
        why = []
        if task["feedback"]:
            why.append(f"Your last attempt was not accepted:\n{task['feedback']}")
        err = task["last_error"] or ""
        if err and not err.startswith(NOT_ACCEPTED):
            why.append(f"Your last run stopped before it finished: {err}")
        return "\n\n".join(why)

    def _agent(self, task, executor, prompt, cwd) -> str:
        """Run an agent, continuing its earlier session when the backend can (session_args in
        its config) and this task has one, so a retry picks up where the last run stopped."""
        c, tid = self.cfg["backends"].get(executor, {}), task["id"]
        contract = CONTRACT.format(minutes=max(self.cfg["limits"]["check_timeout"] // 60, 1))
        if push.asked(task["prompt"]):
            contract += "\n\n" + push.NOTE
        system = AGENT_SYS + (f" {c['notes']}" if c.get("notes") else "") + "\n\n" + contract
        fresh = c.get("session_args")
        resume = c.get("resume_args", fresh)
        def fill(args, sid):
            return [a.replace("{session}", sid).replace("{session_hex}", sid.replace("-", ""))
                    for a in args]
        held, _, sid = (task["session"] or "").partition(" ")
        if resume and sid and held == executor:
            why = self._why(task) or ("The task was queued again, and may have been edited. "
                                      "Check your earlier work against it and finish what's left.")
            try:
                return self.call(executor, tid, "execute",
                                 f"{CONTINUE}\n\n{why}\n\n{prompt}\n\n{contract}", None,
                                 cwd=str(cwd), args=fill(resume, sid))
            except BackendError as e:
                if "no conversation found" not in str(e).lower():
                    raise
                self.log(f"  #{tid}: session {sid} is gone, starting a new one")
        args = []
        if fresh:
            sid = str(uuid.uuid4())
            db.update(self.conn, tid, session=f"{executor} {sid}")  # before: a timeout keeps it
            args = fill(fresh, sid)
        if why := self._why(task):
            prompt += f"\n\nEarlier attempts left their files in this workspace.\n\n{why}"
        return self.call(executor, tid, "execute", prompt, system, cwd=str(cwd), args=args)

    def _verify(self, executor, work: Path, answer: str) -> tuple[bool, str, str]:
        """Hold the agent to its STATUS and CHECK lines. Returns (accepted, a note for the
        result file, feedback for the next attempt if not accepted)."""
        status, check = verify.contract(answer)
        if status is None:
            return (False, "The reply didn't end with a STATUS line.",
                    "Your reply didn't end with the STATUS and CHECK lines, so the work couldn't "
                    "be accepted. Finish the task, then end with them.")
        if status == "INCOMPLETE":
            return (False, "The agent reported STATUS: INCOMPLETE.",
                    "You reported STATUS: INCOMPLETE. Finish what's left. Your summary then "
                    f"ended:\n{answer.strip()[-1500:]}")
        if check is None or not check:
            return True, ("No check command was given, so nothing was re-run." if check is None
                          else "The agent said nothing could be checked by a command (CHECK: none)."), ""
        limit = self.cfg["limits"]["check_timeout"]
        env = backends.environ(self.cfg["backends"].get(executor, {}), str(work))
        try:
            code, out = verify.run_check(check, work, env, limit, verify.secrets(self.cfg))
        except OSError as e:
            return True, f"The check `{check}` was not run: {e}", ""
        what = f"timed out after {limit} s" if code is None else f"exited {code}"
        note = f"`{check}` {what}.\n\n```\n{out or '(no output)'}\n```"
        if code == 0:
            return True, note, ""
        return (False, note, f"You reported STATUS: DONE, but your check `{check}` {what} when "
                f"night-elves ran it. Fix the work (or the check, if the check is wrong). Its "
                f"output ended:\n{out}")

    def _write_result(self, task, executor, answer, work, review="", reviewer=None, check=None,
                      accepted=True, pushed=""):
        tid = task["id"]
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
                attempt = f" (attempt {task['attempts'] + 1})" if task["attempts"] else ""
                files += "\n" + commit_workspace(work, f"Task {tid}: {title}{attempt}") + "\n"
        attached = ""
        if attachments.files(task):
            attached = (f"Attached, in {attachments.task_dir(self.cfg, tid)}:"
                        + attachments.listing(task) + "\n\n")
        crit = f"Done when: {task['criteria'].strip()}\n\n" if task["criteria"] else ""
        verdict = "" if accepted else ", not accepted"
        path.write_text(f"# Task {tid}\n\n{task['prompt']}\n\n{crit}{attached}"
                        f"## Result ({executor}{verdict})\n\n{answer}\n"
                        + (f"\n## Check\n\n{check}\n" if check else "")
                        + (f"\n## Push\n\n{pushed}\n" if pushed else "")
                        + files
                        + (f"\n## Review ({reviewer})\n\n{review}\n" if review else ""))
        db.update(self.conn, tid, result_path=str(path))

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
        seen = attachments.paths(self.cfg, task, attachments.VISUAL)
        kind = self.cfg["backends"].get(executor, {}).get("type")
        if seen and kind == "openai_compat":  # a local vision model: PDFs go as page images
            send, notes = [], []
            for f, p in zip(attachments.files(task), attachments.paths(self.cfg, task)):
                if f["kind"] == "image":
                    send.append(p)
                elif f["kind"] == "pdf":
                    try:
                        pages = attachments.pdf_pages(self.cfg, task, f)
                    except ValueError as e:
                        raise BackendError(str(e)) from e
                    notes.append(f"{f['name']} ({len(pages)} pages, images "
                                 f"{len(send) + 1}-{len(send) + len(pages)})")
                    send += pages
            if notes:
                prompt += "\n\nThe PDFs are attached as page images: " + "; ".join(notes) + "."
            return prompt, cwd, send
        if seen and kind == "command":
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
