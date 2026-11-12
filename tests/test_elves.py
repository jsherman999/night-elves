import contextlib, html, io, json, os, shutil, subprocess, tempfile, threading, time, unittest
import urllib.error, urllib.request
from unittest import mock
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from elves import attachments, backends, cli, config, db, notify, pages, policy, web, when
from elves.backends import BackendError, Command, Result
from elves.dispatcher import Night, passed

class Fake:
    def __init__(self, reply="ok", usd=0.0, fail=0):
        self.reply, self.usd, self.fail, self.calls = reply, usd, fail, []
    def run(self, prompt, system=None, cwd=None, files=()):
        self.calls.append((prompt, system))
        self.cwd, self.files = cwd, list(files)
        if self.fail:
            self.fail -= 1
            raise BackendError("boom")
        text = self.reply(prompt, system) if callable(self.reply) else self.reply
        return Result(text, 10, 20, self.usd)

def make_cfg(tmp):
    with mock.patch.object(config, "find_config", return_value=None):  # not this machine's
        cfg = config.load()
    cfg["paths"] = {"db": ":memory:", "results": tmp, "work": os.path.join(tmp, "work"),
                    "attachments": os.path.join(tmp, "att")}
    cfg["backends"] = {
        "local": {"type": "openai_compat"},
        "claude_api": {"type": "anthropic_api"},
        "claude_cli": {"type": "command", "argv": ["x"], "window": [1, 5], "max_runs_per_night": 2},
    }
    return cfg

def at(day, hour=2):
    return lambda: datetime(2026, 11, day, hour, tzinfo=timezone.utc)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.conn = db.connect(":memory:")
    def night(self, day, hour=2, **fakes):
        self.fakes = {"local": Fake("local answer"), "claude_api": Fake("PASS\nfine", usd=0.5),
                      "claude_cli": Fake("cli answer"), **fakes}
        return Night(self.cfg, self.conn, self.fakes, now_fn=at(day, hour), log=lambda *_: None)

class PolicyTests(Base):
    def test_cycle_bounds(self):
        self.assertEqual(policy.cycle_bounds(date(2026, 11, 11), 1), (date(2026, 11, 1), date(2026, 12, 1)))
        self.assertEqual(policy.cycle_bounds(date(2026, 1, 3), 15), (date(2025, 12, 15), date(2026, 1, 15)))
        self.assertEqual(policy.cycle_bounds(date(2026, 12, 20), 15), (date(2026, 12, 15), date(2027, 1, 15)))

    def test_modes_and_allowance(self):
        s = policy.snapshot(self.cfg, self.conn, at(5)(), "2026-11-05T00:00:00Z")
        self.assertEqual((s.mode, s.tonight_allowance), ("conserve", 2.0))
        s = policy.snapshot(self.cfg, self.conn, at(18)(), "2026-11-18T00:00:00Z")
        self.assertEqual(s.mode, "pace")
        self.assertAlmostEqual(s.tonight_allowance, 100 / 13 * 1.5)
        s = policy.snapshot(self.cfg, self.conn, at(28)(), "2026-11-28T00:00:00Z")
        self.assertEqual((s.mode, s.tonight_allowance), ("endgame", 15.0))  # capped per night

    def test_spend_reduces_remaining_and_old_cycle_ignored(self):
        db.record_spend(self.conn, "claude_api", 1, "plan", usd=40, ts="2026-11-03T00:00:00Z")
        db.record_spend(self.conn, "claude_api", 1, "plan", usd=99, ts="2026-10-20T00:00:00Z")
        s = policy.snapshot(self.cfg, self.conn, at(18)(), "2026-11-18T00:00:00Z")
        self.assertEqual(s.remaining, 60)

    def test_window(self):
        self.assertTrue(policy.in_window([1, 5], 2))
        self.assertFalse(policy.in_window([1, 5], 5))
        self.assertTrue(policy.in_window([22, 5], 23) and policy.in_window([22, 5], 3))
        self.assertFalse(policy.in_window([22, 5], 12))

class DispatchTests(Base):
    def test_conserve_runs_local_only(self):
        tid = db.add_task(self.conn, "summarise logs")
        n = self.night(5, hour=12)
        self.assertEqual(n.run()["done"], 1)
        self.assertEqual(len(self.fakes["claude_api"].calls), 0)
        t = db.get(self.conn, tid)
        self.assertEqual((t["status"], t["backend"]), ("done", "local"))
        self.assertIn("local answer", open(t["result_path"]).read())

    def test_pace_plans_and_reviews_with_frontier(self):
        db.add_task(self.conn, "write a script")
        api = Fake(lambda p, s: "1. do it" if "planning" in s else "PASS\ngood", usd=0.5)
        n = self.night(18, hour=12, claude_api=api)
        n.run()
        self.assertEqual(len(api.calls), 2)
        self.assertIn("1. do it", self.fakes["local"].calls[0][0])
        self.assertAlmostEqual(db.usd_since(self.conn, "claude_api", n.run_start), 1.0)

    def test_review_fail_feeds_back_then_escalates(self):
        tid = db.add_task(self.conn, "hard thing")
        api = Fake(lambda p, s: "plan" if "planning" in s else
                   ("FAIL\nmissing tests" if "reviewing" in s else "frontier answer"), usd=0.2)
        n = self.night(18, hour=12, claude_api=api)
        n.run()
        t = db.get(self.conn, tid)
        self.assertEqual((t["status"], t["backend"], t["attempts"]), ("done", "claude_api", 2))
        self.assertIn("missing tests", self.fakes["local"].calls[1][0])

    def test_frontier_tier_waits_when_no_budget(self):
        self.cfg["budget"]["monthly_credit_usd"] = 0
        tid = db.add_task(self.conn, "needs opus", tier="frontier")
        stats = self.night(18, hour=12).run()
        self.assertEqual((stats["skipped"], db.get(self.conn, tid)["status"]), (1, "queued"))

    def test_subscription_cli_used_inside_window_with_run_cap(self):
        self.cfg["budget"]["monthly_credit_usd"] = 0
        for i in range(3):
            db.add_task(self.conn, f"t{i}", tier="frontier")
        stats = self.night(18, hour=2).run()
        self.assertEqual((stats["done"], stats["skipped"]), (2, 1))
        self.assertEqual(len(self.fakes["claude_cli"].calls), 2)

    def test_nightly_allowance_stops_api_spend(self):
        self.cfg["budget"]["per_night_cap_usd"] = 1.0
        for i in range(4):
            db.add_task(self.conn, f"t{i}", tier="frontier")
        stats = self.night(18, hour=12, claude_api=Fake("answer", usd=0.6)).run()
        self.assertEqual((stats["done"], stats["skipped"]), (2, 2))

    def test_outage_costs_one_attempt_per_run_and_marks_backend_down(self):
        ids = [db.add_task(self.conn, t, tier="local") for t in "abcd"]
        n = self.night(5, hour=12, local=Fake(fail=99))
        stats = n.run()
        self.assertEqual([db.get(self.conn, i)["attempts"] for i in ids], [1, 1, 1, 0])
        self.assertTrue(all(db.get(self.conn, i)["status"] == "queued" for i in ids))
        self.assertIn("local", n.down)
        self.assertEqual((stats["retried"], stats["failed"]), (3, 0))

    def test_errors_fail_task_after_max_attempts(self):
        tid = db.add_task(self.conn, "a", tier="local")
        db.update(self.conn, tid, attempts=2)
        stats = self.night(5, hour=12, local=Fake(fail=1)).run()
        self.assertEqual((db.get(self.conn, tid)["status"], stats["failed"]), ("failed", 1))

    def test_stale_tasks_requeued_and_digest(self):
        tid = db.add_task(self.conn, "interrupted")
        db.update(self.conn, tid, status="running")
        n = self.night(5, hour=12)
        stats = n.run()
        self.assertEqual(db.get(self.conn, tid)["status"], "done")
        text = notify.digest(self.cfg, self.conn, stats["run_start"], at(5)())
        self.assertIn("1 done, 0 failed", text)
        self.assertNotIn("/t/", text)
        self.cfg["web"]["public_url"] = "https://studio.example.ts.net/"
        text = notify.digest(self.cfg, self.conn, stats["run_start"], at(5)())
        self.assertIn(f"interrupted\n  https://studio.example.ts.net/t/{tid}", text)

    def test_agent_tier_runs_in_its_workspace(self):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        tid = db.add_task(self.conn, "capture something", tier="agent")
        def work(prompt, system):
            open(os.path.join(agent.cwd, "out.json"), "w").write("{}")
            for d in ("node_modules/x", ".cache"):
                os.makedirs(os.path.join(agent.cwd, d))
                open(os.path.join(agent.cwd, d, "junk"), "w").write("")
            return "saved out.json"
        agent = Fake(work)
        stats = self.night(18, hour=2, claude_agent=agent).run()
        t = db.get(self.conn, tid)
        self.assertEqual((stats["done"], t["backend"]), (1, "claude_agent"))
        self.assertEqual(agent.cwd, os.path.join(self.tmp, "work", str(tid)))
        self.assertIn("unattended", agent.calls[0][1])
        listing = open(t["result_path"]).read()
        self.assertIn("- out.json", listing)
        self.assertNotIn("junk", listing)
        self.assertIn("Committed to git", listing)
        tracked = subprocess.run(["git", "-C", agent.cwd, "ls-files"], capture_output=True,
                                 text=True, check=True).stdout.split()
        self.assertEqual(tracked, ["out.json"])  # caches and node_modules stay out
        self.assertEqual(len(self.fakes["claude_api"].calls), 0)  # no planner or reviewer

    def test_agent_tier_waits_outside_window_and_auto_never_uses_agent(self):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        a = db.add_task(self.conn, "agent job", tier="agent")
        db.add_task(self.conn, "normal job")
        agent = Fake("should not run")
        stats = self.night(18, hour=12, claude_agent=agent).run()
        self.assertEqual((stats["skipped"], stats["done"], db.get(self.conn, a)["status"]),
                         (1, 1, "queued"))
        self.assertEqual(agent.calls, [])

    def test_passed(self):
        self.assertTrue(passed("PASS\nok") and passed("\n pass"))
        self.assertFalse(passed("FAIL: nope"))

class ScheduleTests(Base):
    def clock(self, day, hour, minute=0):
        self.t = datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)
        return lambda: self.t

    def night_at(self, day, hour, minute=0, **fakes):
        self.fakes = {"local": Fake("local answer"), "claude_api": Fake("PASS\nfine", usd=0.5),
                      "claude_cli": Fake("cli answer"), **fakes}
        return Night(self.cfg, self.conn, self.fakes, now_fn=self.clock(day, hour, minute),
                     log=lambda *_: None)

    def test_parse_at(self):
        now = datetime(2026, 11, 12, 0, 45, tzinfo=timezone.utc)
        self.assertEqual(when.parse_at("01:30", now), now.replace(hour=1, minute=30))
        self.assertEqual(when.parse_at("12:15am", now), datetime(2026, 11, 13, 0, 15, tzinfo=timezone.utc))
        self.assertEqual(when.parse_at("2 pm", now).hour, 14)
        self.assertEqual(when.parse_at("12pm", now).hour, 12)
        self.assertEqual(when.parse_at("2026-11-20 7:05", now), datetime(2026, 11, 20, 7, 5, tzinfo=timezone.utc))
        for bad in ("5", "25:00", "13pm", "tomorrow"):
            with self.assertRaises(ValueError):
                when.parse_at(bad, now)

    def test_scheduled_task_waits_and_due_run_ignores_the_rest(self):
        plain = db.add_task(self.conn, "plain")
        timed = db.add_task(self.conn, "timed", run_after="2026-11-05T02:00:00Z")
        stats = self.night_at(5, 1).run()
        self.assertEqual((stats["done"], db.get(self.conn, timed)["status"]), (1, "queued"))
        later = db.add_task(self.conn, "plain, added later")
        stats = self.night_at(5, 2, 1).run(due_only=True)
        self.assertEqual(stats["done"], 1)
        self.assertEqual([db.get(self.conn, i)["status"] for i in (plain, timed, later)],
                         ["done", "done", "queued"])

    def test_due_tasks_go_first(self):
        db.add_task(self.conn, "urgent but plain", priority=1)
        timed = db.add_task(self.conn, "timed", priority=9, run_after="2026-11-05T02:00:00Z")
        n = self.night_at(5, 3)
        n.run(max_tasks=1)
        self.assertEqual(db.get(self.conn, timed)["status"], "done")

    def test_error_pushes_scheduled_task_back(self):
        tid = db.add_task(self.conn, "timed", tier="local", run_after="2026-11-05T02:00:00Z")
        self.night_at(5, 2, 5, local=Fake(fail=1)).run(due_only=True)
        t = db.get(self.conn, tid)
        self.assertEqual((t["status"], t["attempts"], t["run_after"]), ("queued", 1, "2026-11-05T02:35:00Z"))

    def test_nightly_cap_is_shared_by_runs_on_the_same_day(self):
        self.cfg["budget"]["per_night_cap_usd"] = 1.0
        for i in range(3):
            db.add_task(self.conn, f"t{i}", tier="frontier")
        api = Fake("answer", usd=0.6)
        self.night_at(18, 12, claude_api=api).run(max_tasks=1)
        stats = self.night_at(18, 15, claude_api=api).run()
        self.assertEqual((stats["done"], stats["skipped"]), (1, 1))

    def test_cli_add_at_and_list(self):
        path = os.path.join(self.tmp, "c.toml")
        open(path, "w").write(f'[paths]\ndb = "{self.tmp}/q.db"\nresults = "{self.tmp}"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["-c", path, "add", "--at", "03:15", "scheduled thing"]), 0)
            cli.main(["-c", path, "list"])
        self.assertRegex(out.getvalue(), r"queued #1 for \w{3} 03:15\n#1 .* @\w{3} 03:15 scheduled thing")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["-c", path, "add", "--at", "later", "x"]), 2)

class ChangeTests(Base):
    def test_edit_cancel_requeue_rules(self):
        tid = db.add_task(self.conn, "first draft")
        db.change(self.conn, tid, prompt="  second draft ", tier="agent", priority=2)
        t = db.get(self.conn, tid)
        self.assertEqual((t["prompt"], t["tier"], t["priority"]), ("second draft", "agent", 2))
        with self.assertRaises(ValueError):
            db.change(self.conn, tid, tier="psychic")
        with self.assertRaises(ValueError):
            db.change(self.conn, tid, priority=0)
        with self.assertRaises(LookupError):
            db.cancel(self.conn, 99)
        db.cancel(self.conn, tid)
        self.assertEqual(db.get(self.conn, tid)["status"], "cancelled")
        self.assertIsNone(db.next_task(self.conn))
        db.requeue(self.conn, tid)
        self.assertEqual(db.get(self.conn, tid)["status"], "queued")
        db.update(self.conn, tid, status="running")
        for change in (lambda: db.cancel(self.conn, tid), lambda: db.requeue(self.conn, tid),
                       lambda: db.change(self.conn, tid, prompt="x")):
            with self.assertRaisesRegex(ValueError, "is running"):
                change()

    def test_task_cancelled_after_being_picked_is_not_run(self):
        tid = db.add_task(self.conn, "picked then cancelled")
        n = self.night(5, hour=12)
        task = db.next_task(self.conn)
        db.cancel(self.conn, tid)
        self.assertEqual(n.process(task), "skipped")
        self.assertEqual(self.fakes["local"].calls, [])
        self.assertEqual(db.get(self.conn, tid)["status"], "cancelled")

    def test_cli_edit_cancel_retry(self):
        path = os.path.join(self.tmp, "c.toml")
        open(path, "w").write(f'[paths]\ndb = "{self.tmp}/q.db"\nresults = "{self.tmp}"\n')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(["-c", path, "add", "--at", "03:15", "scheduled thing"])
            cli.main(["-c", path, "add", "other"])
            self.assertEqual(cli.main(["-c", path, "edit", "1", "-t", "agent", "-p", "1",
                                       "--prompt", "new words", "--tonight"]), 0)
            self.assertEqual(cli.main(["-c", path, "edit", "2", "--now"]), 0)
            self.assertEqual(cli.main(["-c", path, "edit", "2"]), 2)
            self.assertEqual(cli.main(["-c", path, "cancel", "1", "2"]), 0)
            self.assertEqual(cli.main(["-c", path, "cancel", "7"]), 1)
            self.assertEqual(cli.main(["-c", path, "retry", "2"]), 0)
        conn = db.connect(f"{self.tmp}/q.db")
        one, two = db.get(conn, 1), db.get(conn, 2)
        self.assertEqual((one["status"], one["tier"], one["priority"], one["prompt"], one["run_after"]),
                         ("cancelled", "agent", 1, "new words", None))
        self.assertEqual(two["status"], "queued")
        self.assertIsNotNone(two["run_after"])
        self.assertIn("no task #7", err.getvalue())
        self.assertIn("cancelled #2", out.getvalue())

class MiscTests(Base):
    def test_command_backend_arg_and_stdin(self):
        self.assertEqual(Command("e", {"argv": ["echo", "{prompt}"]}).run("hi; rm -rf /").text, "hi; rm -rf /")
        self.assertEqual(Command("c", {"argv": ["cat"]}).run("via stdin", "sys").text, "sys\n\nvia stdin")
        with self.assertRaises(BackendError):
            Command("f", {"argv": ["false"]}).run("x")

    def test_command_backend_drops_api_key(self):
        sh = ["sh", "-c", 'echo "${ANTHROPIC_API_KEY:-unset} ${ELVES_KEEP:-unset}"']
        with mock.patch.dict(os.environ, ANTHROPIC_API_KEY="sk-test", ELVES_KEEP="kept",
                             ELVES_TOKEN="t"):
            self.assertEqual(Command("c", {"argv": sh}).run("x").text, "unset kept")
            self.assertEqual(Command("c", {"argv": sh, "env_unset": []}).run("x").text, "sk-test kept")
            tok = Command("c", {"argv": ["sh", "-c", 'echo "${ELVES_TOKEN:-unset}"']})
            self.assertEqual(tok.run("x").text, "unset")

    def test_command_backend_env_with_workspace(self):
        c = Command("c", {"argv": ["sh", "-c", 'echo "$UV_CACHE_DIR|${PLAIN:-}"'],
                          "env": {"UV_CACHE_DIR": "{cwd}/.cache/uv", "PLAIN": "yes"}})
        self.assertEqual(c.run("x", cwd=self.tmp).text, f"{self.tmp}/.cache/uv|yes")
        with mock.patch.dict(os.environ, {"UV_CACHE_DIR": "outer"}):
            self.assertEqual(c.run("x").text, "outer|yes")  # no workspace: {cwd} entries skipped

    def test_command_backend_cwd_and_timeout_kills_children(self):
        self.assertEqual(Command("p", {"argv": ["pwd"]}).run("x", cwd=self.tmp).text,
                         os.path.realpath(self.tmp))
        mark = os.path.join(self.tmp, "mark")
        slow = Command("s", {"argv": ["sh", "-c", f"(sleep 1; touch {mark}) & sleep 10"],
                             "timeout": 0.3})
        with self.assertRaises(BackendError):
            slow.run("x")
        time.sleep(1.5)
        self.assertFalse(os.path.exists(mark))  # the background child was killed too

    def test_notify_failure_does_not_raise(self):
        self.cfg["notify"] = {"argv": ["sleep", "5"], "timeout": 0.2}
        notify.send(self.cfg, "digest")  # timeout is reported, not raised
        self.cfg["notify"] = {"argv": ["/nonexistent/notifier", "{text}"]}
        notify.send(self.cfg, "digest")
        self.assertTrue(any(f.startswith("digest-") for f in os.listdir(self.tmp)))

    def test_parse_inline(self):
        self.assertEqual(web.parse_inline("! opus: fix it"), ("fix it", 1, "frontier", None))
        self.assertEqual(web.parse_inline("plain"), ("plain", 5, "auto", None))
        self.assertEqual(web.parse_inline("Agent: fetch it"), ("fetch it", 5, "agent", None))
        self.assertEqual(web.parse_inline("at 1:30am agent: capture"), ("capture", 5, "agent", "1:30am"))
        self.assertEqual(web.parse_inline("agent: At 2 PM CST, scan it"), ("scan it", 5, "agent", "2 PM"))
        self.assertEqual(web.parse_inline("! at 01:30: do it"), ("do it", 1, "auto", "01:30"))
        self.assertEqual(web.parse_inline("At 5 companies, compare"), ("At 5 companies, compare", 5, "auto", None))
        self.assertEqual(web.parse_inline("at 1:30 make tea"), ("make tea", 5, "auto", "1:30"))

    def test_web_intake(self):
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        self.cfg["web"]["sms_allow"] = ["+15550001111"]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg, "s3cret-token-123456"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        def post(path, data, headers):
            req = urllib.request.Request(base + path, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        js = {"Content-Type": "application/json"}
        body = json.dumps({"prompt": "from web", "priority": 2}).encode()
        self.assertEqual(post("/tasks", body, js)[0], 401)
        self.assertEqual(post("/tasks", body, {**js, "Authorization": "Bearer s3cret-token-123456"})[0], 201)
        self.assertEqual(post("/tasks", b"{}", {**js, "Authorization": "Bearer s3cret-token-123456"})[0], 400)
        shorthand = json.dumps({"prompt": "agent: run it", "at": "2026-12-01 01:30"}).encode()
        code, text = post("/tasks", shorthand, {**js, "Authorization": "Bearer s3cret-token-123456"})
        self.assertEqual(code, 201)
        self.assertTrue(json.loads(text)["message"].startswith("Queued #2 for "))
        bad = json.dumps({"prompt": "x", "at": "soonish"}).encode()
        self.assertEqual(post("/tasks", bad, {**js, "Authorization": "Bearer s3cret-token-123456"})[0], 400)
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(post("/sms?key=s3cret-token-123456", b"From=%2B19990000000&Body=hi", form)[0], 401)
        code, text = post("/sms?key=s3cret-token-123456", b"From=%2B15550001111&Body=%21urgent", form)
        self.assertEqual(code, 200)
        self.assertIn("Queued #3", text)
        srv.shutdown()
        rows = db.list_tasks(db.connect(self.cfg["paths"]["db"]))
        self.assertIsNotNone(rows[1]["run_after"])
        self.assertEqual([(r["source"], r["priority"], r["tier"], r["prompt"]) for r in rows],
                         [("sms", 1, "auto", "urgent"), ("web", 5, "agent", "run it"),
                          ("web", 2, "auto", "from web")])

    def test_phone_pages(self):
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        conn = db.connect(self.cfg["paths"]["db"])
        done = db.add_task(conn, "finished <b>thing</b>")
        res = os.path.join(self.tmp, f"{done}.md")
        open(res, "w").write("# the answer <script>")
        db.update(conn, done, status="done", result_path=res)
        db.update(conn, db.add_task(conn, "elsewhere"), status="done", result_path="/etc/hosts")
        waiting = db.add_task(conn, "waiting one", run_after="2030-01-01T07:30:00Z")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg, "s3cret-token-123456"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        def call(path, data=None, headers=None):
            req = urllib.request.Request(base + path, data=data, headers=headers or {})
            try:
                with opener.open(req) as r:
                    return r.status, r.read().decode(errors="replace"), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(), e.headers
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertIn("Sign in", call("/")[1])
        code, _, h = call("/apple-touch-icon.png")
        self.assertEqual((code, h["Content-Type"]), (200, "image/png"))
        self.assertEqual(call("/login", b"token=wrong", form)[0], 401)
        code, _, h = call("/login", b"token=s3cret-token-123456", form)
        self.assertEqual(code, 303)
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertNotIn("s3cret", cookie)
        me = {"Cookie": cookie}
        code, page, h = call("/", headers=me)
        self.assertEqual(code, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertIn("finished &lt;b&gt;thing&lt;/b&gt;", page)
        self.assertIn(f"href=/t/{waiting}", page)
        self.assertIn(f"id=tierhelp>{html.escape(pages.TIER_HELP['auto'])}<", page)
        page = call(f"/t/{done}", headers=me)[1]
        self.assertIn("<h1>the answer &lt;script&gt;</h1>", page)
        self.assertIn("# the answer &lt;script&gt;", call(f"/t/{done}?raw=1", headers=me)[1])
        self.assertNotIn("localhost", call(f"/t/{done + 1}", headers=me)[1])  # outside results/
        self.assertEqual(call(f"/t/{done}")[0], 303)  # not signed in
        # changes: from the page with the cookie, and as JSON with the token
        code, _, h = call(f"/tasks/{waiting}", b"action=save&prompt=edited&tier=local&priority=2&at=",
                          {**form, **me, "Origin": base})
        self.assertEqual((code, h["Location"]), (303, f"/t/{waiting}?m=Saved%20%23{waiting}"))
        t = db.get(conn, waiting)
        self.assertEqual((t["prompt"], t["tier"], t["priority"], t["run_after"]), ("edited", "local", 2, None))
        self.assertEqual(call(f"/tasks/{waiting}", b"action=cancel",
                              {**form, **me, "Origin": "https://evil.example"})[0], 401)
        self.assertEqual(call(f"/tasks/{waiting}", b"action=cancel", form)[0], 401)
        auth = {"Content-Type": "application/json", "Authorization": "Bearer s3cret-token-123456"}
        code, text, _ = call(f"/tasks/{waiting}", json.dumps({"action": "now"}).encode(), auth)
        self.assertEqual(code, 200)
        self.assertIn("within a minute", json.loads(text)["message"])
        self.assertEqual(call(f"/tasks/{waiting}", b'{"action": "cancel"}', auth)[0], 200)
        self.assertEqual(call(f"/tasks/{waiting}", b'{"action": "cancel"}', auth)[0], 409)
        self.assertEqual(call("/tasks/999", b'{"action": "cancel"}', auth)[0], 404)
        self.assertEqual(db.get(conn, waiting)["status"], "cancelled")
        code, text, _ = call(f"/tasks/{done}", headers={"Authorization": "Bearer s3cret-token-123456"})
        self.assertEqual(json.loads(text)["result"], "# the answer <script>")
        code, _, h = call("/tasks", b"prompt=agent%3A+from+the+page&priority=5&at=", {**form, **me})
        self.assertEqual((code, h["Location"]), (303, "/?m=Queued%20%234"))
        self.assertEqual(db.get(conn, 4)["tier"], "agent")
        srv.shutdown()

if __name__ == "__main__":
    unittest.main()


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81"
       b"\x84\x00\x00\x00\x00IEND\xaeB`\x82")  # 1x1

def multipart(fields: dict, files: list[tuple[str, bytes]], field="files") -> tuple[bytes, str]:
    b = "XyZboundary"
    out = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                   for k, v in fields.items())
    for name, data in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"

class FileTests(Base):
    def stage(self, *items):
        st = attachments.Staged(self.cfg)
        for name, data in items:
            open(st.new_file(name), "wb").write(data)
        st.finish()
        return st

    def add(self, *items, **kw):
        with self.stage(*items) as st:
            return attachments.add_task(self.cfg, self.conn, kw.pop("prompt", "do it"), st, **kw)

    def test_kinds_and_names(self):
        st = self.stage(("shot.png", PNG), ("../../etc/notes.txt", b"hello\n"),
                        ("big.csv", b"a,b\n" * 60_000), ("doc.pdf", b"%PDF-1.7 ..."),
                        ("blob.bin", b"\x00\x01\x02"), ("empty.txt", b""), (None, b"x y"))
        self.assertEqual([(f["name"], f["kind"]) for f in st.files],
                         [("1-shot.png", "image"), ("2-notes.txt", "text"), ("3-big.csv", "data"),
                          ("4-doc.pdf", "pdf"), ("5-blob.bin", "data"), ("6-file", "text")])
        self.assertEqual(sorted(os.listdir(st.dir)), sorted(f["name"] for f in st.files))
        st.discard()
        self.assertEqual(attachments.sniff(b"\x00\x00\x00\x18ftypheic...."), "heic")

    def test_text_over_the_inline_budget_becomes_data(self):
        half = b"x" * (attachments.INLINE_MAX // 2 + 1)
        with self.stage(("a.txt", half), ("b.txt", half)) as st:
            self.assertEqual([f["kind"] for f in st.files], ["text", "data"])

    def test_tier_rules(self):
        tid = self.add(("shot.png", PNG))
        with self.assertRaisesRegex(ValueError, "local model"):
            db.change(self.conn, tid, tier="local")
        with self.assertRaisesRegex(ValueError, "local model"):
            self.add(("shot.png", PNG), tier="local")
        with self.assertRaisesRegex(ValueError, "agent tier"):
            self.add(("blob.bin", b"\x00\x01"))
        data = self.add(("blob.bin", b"\x00\x01"), tier="agent")
        with self.assertRaisesRegex(ValueError, "agent tier"):
            db.change(self.conn, data, tier="frontier")
        self.add(("notes.txt", b"plain"), tier="local")  # text is fine anywhere
        self.assertEqual(len(db.list_tasks(self.conn)), 3)
        self.assertEqual(sorted(os.listdir(self.cfg["paths"]["attachments"])),
                         sorted(str(t["id"]) for t in db.list_tasks(self.conn)))  # no strays

    def test_failed_move_adds_nothing(self):
        def boom(tid):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            db.add_task(self.conn, "x", files=[{"name": "1-a", "kind": "text", "size": 1}],
                        on_insert=boom)
        self.assertEqual(db.list_tasks(self.conn), [])

    def test_old_image_column_is_migrated(self):
        path = os.path.join(self.tmp, "old.db")
        conn = db.connect(path)
        conn.execute("INSERT INTO tasks (created_at, updated_at, prompt, images) VALUES "
                     "('t', 't', 'x', '[\"1.png\"]')")
        conn.commit()
        conn.close()
        t = db.list_tasks(db.connect(path))[0]
        self.assertEqual(attachments.files(t), [{"name": "1.png", "kind": "image", "size": None}])

    def test_text_is_pasted_in_for_the_local_model(self):
        tid = self.add(("notes.txt", b"the secret word is plum"))
        self.night(5, hour=12).run()
        self.assertIn("the secret word is plum", self.fakes["local"].calls[0][0])
        self.assertIn("1-notes.txt", open(db.get(self.conn, tid)["result_path"]).read())

    def test_images_and_pdfs_go_to_the_api(self):
        self.add(("shot.png", PNG), ("doc.pdf", b"%PDF-1.7"), ("n.txt", b"see attached"))
        self.night(5, hour=12).run()  # conserve mode would normally stay local
        api = self.fakes["claude_api"]
        self.assertEqual(self.fakes["local"].calls, [])
        self.assertEqual([p.name for p in api.files], ["1-shot.png", "2-doc.pdf"])
        self.assertIn("see attached", api.calls[0][0])

    def test_cli_fallback_runs_in_the_files_folder(self):
        tid = self.add(("shot.png", PNG), tier="frontier")
        self.cfg["budget"]["conserve_night_cap_usd"] = 0  # no API allowance tonight
        self.night(5, hour=2).run()
        cli_ = self.fakes["claude_cli"]
        folder = os.path.join(self.cfg["paths"]["attachments"], str(tid))
        self.assertEqual((cli_.cwd, cli_.files), (folder, []))
        self.assertIn(os.path.join(folder, "1-shot.png"), cli_.calls[0][0])
        self.assertIn("Read tool", cli_.calls[0][0])

    def test_agent_gets_copies_in_its_workspace(self):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        tid = self.add(("big.bin", b"\x00" * 1000), ("shot.png", PNG), tier="agent")
        def work(prompt, system):
            open(os.path.join(agent.cwd, "input", "1-big.bin"), "wb").write(b"changed")
            return "done"
        agent = Fake(work)
        self.night(18, hour=2, claude_agent=agent).run()
        self.assertIn("input/1-big.bin (data, 1000 bytes)", agent.calls[0][0])
        self.assertEqual(agent.files, [])
        original = attachments.paths(self.cfg, db.get(self.conn, tid))[0]
        self.assertEqual(original.read_bytes(), b"\x00" * 1000)  # the agent changed only its copy

    def test_missing_file_is_an_error_not_a_blind_answer(self):
        tid = self.add(("notes.txt", b"x"))
        os.remove(attachments.paths(self.cfg, db.get(self.conn, tid))[0])
        self.night(18, hour=12).run()
        self.assertIn("missing", db.get(self.conn, tid)["last_error"])

    def test_backends_attach_files(self):
        from pathlib import Path
        png, pdf = Path(self.tmp, "1.png"), Path(self.tmp, "2.pdf")
        png.write_bytes(PNG)
        pdf.write_bytes(b"%PDF-1.7")
        api = backends.AnthropicAPI("claude_api", {"model": "m"})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(backends, "_post", return_value={"content": [{"type": "text", "text": "ok"}]}) as post:
            self.assertEqual(api.run("describe", files=[png, pdf]).text, "ok")
        content = post.call_args[0][2]["messages"][0]["content"]
        self.assertEqual([(c["type"], c.get("source", {}).get("media_type")) for c in content],
                         [("image", "image/png"), ("document", "application/pdf"), ("text", None)])
        with self.assertRaises(BackendError):
            backends.OpenAICompat("local", {"model": "m", "base_url": "x"}).run("x", files=[png])

    def test_multipart_streaming(self):
        import io
        big = os.urandom(3 * 1024 * 1024 + 7) + b"\r\n--XyZ"  # spans chunks, looks boundary-ish
        body, ctype = multipart({"prompt": "héllo", "tier": "agent"}, [("d.bin", big), ("", b"")])
        st = attachments.Staged(self.cfg)
        d = web.read_multipart(io.BytesIO(body), len(body), "XyZboundary", st.new_file)
        self.assertEqual((d["prompt"], d["tier"]), ("héllo", "agent"))
        st.finish()
        self.assertEqual(len(st.files), 1)
        self.assertEqual((st.dir / st.files[0]["name"]).read_bytes(), big)
        st.discard()
        with self.assertRaisesRegex(ValueError, "ended early"):
            web.read_multipart(io.BytesIO(body[:-100]), len(body), "XyZboundary",
                               attachments.Staged(self.cfg).new_file)

    def test_cli_add_file(self):
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        src = os.path.join(self.tmp, "report.csv")
        open(src, "w").write("a,b\n1,2\n")
        with mock.patch.object(config, "load", return_value=self.cfg), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["add", "-f", src, "sum column b"]), 0)
        self.assertIn("with 1 file", out.getvalue())
        t = db.list_tasks(db.connect(self.cfg["paths"]["db"]))[0]
        self.assertEqual(attachments.files(t)[0]["name"], "1-report.csv")
        with mock.patch.object(config, "load", return_value=self.cfg), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["add", "-f", os.path.join(self.tmp, "nope"), "x"]), 2)

    def test_web_upload_and_view(self):
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg, "s3cret-token-123456"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        def call(path, data=None, headers=None):
            req = urllib.request.Request(base + path, data=data, headers=headers or {})
            try:
                with opener.open(req) as r:
                    return r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers
        files = [("shot.png", PNG), ("page.html", b"<script>alert(1)</script>"),
                 ("data.bin", b"\x00\x01")]
        body, ctype = multipart({"prompt": "make an app like this", "tier": "agent"}, files)
        self.assertEqual(call("/tasks", body, {"Content-Type": ctype})[0], 401)
        bearer = {"Authorization": "Bearer s3cret-token-123456"}
        code, out, _ = call("/tasks", body, {"Content-Type": ctype, **bearer})  # a Shortcut
        self.assertEqual((code, json.loads(out)["files"]), (201, 3))
        old, ctype2 = multipart({"prompt": "0.4 shortcut"}, [("s.png", PNG)], field="images")
        self.assertEqual(call("/tasks", old, {"Content-Type": ctype2, **bearer})[0], 201)
        auto, ctype3 = multipart({"prompt": "x"}, [("data.bin", b"\x00")])
        code, out, _ = call("/tasks", auto, {"Content-Type": ctype3, **bearer})
        self.assertEqual(code, 400)
        self.assertIn("agent tier", json.loads(out)["error"])
        cookie = call("/login", b"token=s3cret-token-123456",
                      {"Content-Type": "application/x-www-form-urlencoded"})[2]["Set-Cookie"].split(";")[0]
        me = {"Cookie": cookie}
        code, _, h = call("/tasks", auto, {"Content-Type": ctype3, **me, "Origin": base})  # the form
        self.assertEqual((code, h["Location"][:15]), (303, "/?m=Not%20queue"))
        empty, ctype4 = multipart({"prompt": "no file"}, [("", b"")])  # form sent with none chosen
        self.assertEqual(call("/tasks", empty, {"Content-Type": ctype4, **me})[0], 303)
        page = call("/t/1", headers=me)[1].decode()
        self.assertIn("<img src=/t/1/f/1", page)
        self.assertIn("2-page.html", page)
        self.assertIn("3 files", call("/", headers=me)[1].decode())
        code, img, h = call("/t/1/f/1", headers=me)
        self.assertEqual((code, img, h["Content-Type"]), (200, PNG, "image/png"))
        code, _, h = call("/t/1/f/2", headers=me)  # HTML is shown as text, never run
        self.assertEqual(h["Content-Type"], "text/plain; charset=utf-8")
        code, _, h = call("/t/1/f/3", headers=me)
        self.assertEqual((h["Content-Type"], h["Content-Disposition"]),
                         ("application/octet-stream", 'attachment; filename="3-data.bin"'))
        self.assertEqual(call("/t/1/f/1")[0], 303)  # not signed in
        self.assertEqual(call("/t/1/f/4", headers=me)[0], 404)
        self.assertEqual(call("/tasks/1/f/1", headers=bearer)[0], 200)
        self.assertEqual([f["kind"] for f in json.loads(call("/tasks/1", headers=bearer)[1])["files"]],
                         ["image", "text", "data"])
        srv.shutdown()
        rows = db.list_tasks(db.connect(self.cfg["paths"]["db"]))
        self.assertEqual([(r["prompt"], len(attachments.files(r))) for r in rows],
                         [("no file", 0), ("0.4 shortcut", 1), ("make an app like this", 3)])

def make_pdf(pages: int) -> bytes:
    """A minimal valid PDF with a line of text on each page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{3 + 2 * i} 0 R" for i in range(pages)), pages)]
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> >>")
        stream = f"BT /F1 24 Tf 72 700 Td (Page {i + 1}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)

class LocalVisionTests(Base):
    stage = FileTests.stage
    def setUp(self):
        super().setUp()
        self.cfg["backends"]["local_vision"] = {"type": "openai_compat", "vision": True}
        self.cfg["roles"]["local_vision"] = "local_vision"

    def night(self, day, hour=2, **fakes):
        return super().night(day, hour, local_vision=Fake("i see it"), **fakes)

    def test_images_go_to_the_local_vision_model(self):
        tid = self.add(("shot.png", PNG), tier="local")  # allowed now
        self.night(18, hour=12).run()
        v = self.fakes["local_vision"]
        self.assertEqual([p.name for p in v.files], ["1-shot.png"])
        self.assertEqual(self.fakes["claude_api"].calls, [])  # no planner or reviewer
        self.assertEqual(db.get(self.conn, tid)["backend"], "local_vision")

    def test_auto_prefers_local_vision_then_escalates(self):
        tid = self.add(("shot.png", PNG))
        db.update(self.conn, tid, attempts=2)
        self.night(18, hour=12).run()
        self.assertEqual(db.get(self.conn, tid)["backend"], "claude_api")
        self.assertEqual(self.fakes["local_vision"].calls, [])

    def test_frontier_and_endgame_still_use_claude(self):
        a = self.add(("shot.png", PNG), tier="frontier")
        b = self.add(("shot.png", PNG), priority=1)
        self.night(28, hour=12).run()  # endgame: hard tasks go to Claude
        self.assertEqual([db.get(self.conn, t)["backend"] for t in (a, b)], ["claude_api"] * 2)

    def test_without_vision_backend_local_is_refused(self):
        self.cfg["roles"]["local_vision"] = ""
        with self.assertRaisesRegex(ValueError, "can't see images"):
            self.add(("shot.png", PNG), tier="local", vision=False)

    def add(self, *items, **kw):
        kw.setdefault("vision", attachments.local_vision(self.cfg))
        with self.stage(*items) as st:
            return db.add_task(self.conn, kw.pop("prompt", "do it"), files=st.files,
                               on_insert=st.commit, **kw)

    def test_long_pdf_is_refused_for_local_and_goes_to_claude_on_auto(self):
        files = [{"name": "1-a.pdf", "kind": "pdf", "size": 9, "pages": 40}]
        with self.assertRaisesRegex(ValueError, "up to 20 pages"):
            attachments.check_tier("local", files, vision=True)
        attachments.check_tier("auto", files, vision=True)
        self.assertFalse(attachments.local_can_see(files))
        self.assertFalse(attachments.local_can_see([{"name": "x", "kind": "pdf", "pages": None}]))

    @unittest.skipUnless(shutil.which("swiftc"), "needs macOS with swiftc")
    def test_pdf_pages_are_counted_and_rendered(self):
        tid = self.add(("doc.pdf", make_pdf(3)), tier="local")
        t = db.get(self.conn, tid)
        f = attachments.files(t)[0]
        self.assertEqual((f["kind"], f["pages"]), ("pdf", 3))
        self.night(18, hour=12).run()
        v = self.fakes["local_vision"]
        self.assertEqual([p.name for p in v.files], ["page-001.png", "page-002.png", "page-003.png"])
        self.assertTrue(all(attachments.sniff(p.read_bytes()[:16]) == "png" for p in v.files))
        self.assertIn("1-doc.pdf (3 pages, images 1-3)", v.calls[0][0])

    def test_openai_backend_sends_data_urls(self):
        from pathlib import Path
        png = Path(self.tmp, "1.png")
        png.write_bytes(PNG)
        b = backends.OpenAICompat("v", {"model": "m", "base_url": "http://x/v1", "vision": True})
        reply = {"choices": [{"message": {"content": "a dot"}}]}
        with mock.patch.object(backends, "_post", return_value=reply) as post:
            self.assertEqual(b.run("what", files=[png]).text, "a dot")
        content = post.call_args[0][2]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR"))

class MarkdownTests(unittest.TestCase):
    def test_blocks(self):
        from elves import md
        out = md.render("# Title\n\nSome **bold** and `x<y`.\n\n1. one\n2. two\n   - sub\n\n"
                        "- [x] done\n\n| a | b |\n|---|--:|\n| 1 | 2 |\n\n```sh\necho <hi>\n```\n> q")
        for want in ("<h1>Title</h1>", "<strong>bold</strong>", "<code>x&lt;y</code>",
                     "<ol><li>one</li><li>two\n<ul><li>sub</li></ul></li></ol>", "<li>☑ done</li>",
                     "<th>a</th>", "<td style=text-align:right>2</td>",
                     '<pre><code class="lang-sh">echo &lt;hi&gt;</code></pre>', "<blockquote><p>q</p>"):
            self.assertIn(want, out)

    def test_nothing_from_the_source_becomes_markup(self):
        from elves import md
        out = md.render('<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n'
                        '[a](javascript:alert(1)) [b](//evil.example) [c](https://ok.example/p_(1))'
                        ' [d](x" onmouseover="y) https://ok.example/?a=1&b="2"\n\n'
                        '`<b>` **<i>x</i>** | not | a | table |')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("<i>", out)
        self.assertNotIn("javascript:", out.split("href")[0] + "".join(
            h.split(">")[0] for h in out.split("href=")[1:]))
        self.assertNotIn("evil.example\"", out)
        hrefs = [h.split('"')[1] for h in out.split("href=")[1:]]
        self.assertEqual(hrefs, ["https://ok.example/p_(1)", "https://ok.example/?a=1&amp;b=&quot;2"])
        self.assertNotIn("&amp;quot;", out)
        self.assertNotIn("onmouseover=\"", out)
