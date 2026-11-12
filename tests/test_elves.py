import contextlib, html, io, json, os, subprocess, tempfile, threading, time, unittest
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
    def run(self, prompt, system=None, cwd=None, images=()):
        self.calls.append((prompt, system))
        self.cwd, self.images = cwd, list(images)
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

def multipart(fields: dict, files: list[bytes]) -> tuple[bytes, str]:
    b = "XyZboundary"
    out = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                   for k, v in fields.items())
    for i, data in enumerate(files):
        out += (f'--{b}\r\nContent-Disposition: form-data; name="images"; filename="s{i}.png"\r\n'
                f"Content-Type: image/png\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"

class ImageTests(Base):
    def test_sniff(self):
        self.assertEqual(attachments.sniff(PNG), "png")
        self.assertEqual(attachments.sniff(b"\xff\xd8\xff\xe0rest"), "jpg")
        self.assertEqual(attachments.sniff(b"\x00\x00\x00\x18ftypheic...."), "heic")
        self.assertIsNone(attachments.sniff(b"<html>"))

    def test_add_stores_images_and_refuses_local(self):
        tid = attachments.add_task(self.cfg, self.conn, "make this", [PNG, PNG])
        t = db.get(self.conn, tid)
        self.assertEqual(attachments.names(t), ["1.png", "2.png"])
        self.assertTrue(all(p.read_bytes() == PNG for p in attachments.paths(self.cfg, t)))
        with self.assertRaisesRegex(ValueError, "local model"):
            db.change(self.conn, tid, tier="local")
        with self.assertRaisesRegex(ValueError, "local model"):
            attachments.add_task(self.cfg, self.conn, "x", [PNG], tier="local")
        with self.assertRaisesRegex(ValueError, "isn't a PNG"):
            attachments.add_task(self.cfg, self.conn, "x", [b"not an image"])
        with self.assertRaisesRegex(ValueError, "at most"):
            attachments.add_task(self.cfg, self.conn, "x", [PNG] * 5)
        self.assertEqual(len(db.list_tasks(self.conn)), 1)
        self.assertEqual(sorted(os.listdir(self.cfg["paths"]["attachments"])), [str(tid)])  # no strays

    def test_failed_move_adds_nothing(self):
        def boom(tid):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            db.add_task(self.conn, "x", images=["1.png"], on_insert=boom)
        self.assertEqual(db.list_tasks(self.conn), [])

    def test_auto_with_images_goes_to_claude_api(self):
        tid = attachments.add_task(self.cfg, self.conn, "what is this", [PNG])
        n = self.night(5, hour=12)  # conserve mode would normally stay local
        self.assertEqual(n.run()["done"], 1)
        api = self.fakes["claude_api"]
        self.assertEqual(self.fakes["local"].calls, [])
        self.assertEqual([p.name for p in api.images], ["1.png"])
        self.assertIn("1.png", open(db.get(self.conn, tid)["result_path"]).read())

    def test_cli_fallback_runs_in_the_image_folder(self):
        tid = attachments.add_task(self.cfg, self.conn, "what is this", [PNG], tier="frontier")
        self.cfg["budget"]["conserve_night_cap_usd"] = 0  # no API allowance tonight
        n = self.night(5, hour=2)
        self.assertEqual(n.run()["done"], 1)
        cli_ = self.fakes["claude_cli"]
        self.assertEqual(cli_.cwd, os.path.join(self.cfg["paths"]["attachments"], str(tid)))

    def test_agent_gets_copies_in_its_workspace(self):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        tid = attachments.add_task(self.cfg, self.conn, "build this", [PNG], tier="agent")
        agent = Fake("built")
        self.night(18, hour=2, claude_agent=agent).run()
        work = os.path.join(self.tmp, "work", str(tid))
        self.assertEqual([str(p) for p in agent.images], [os.path.join(work, "input", "1.png")])
        self.assertTrue(os.path.isfile(os.path.join(work, "input", "1.png")))

    def test_missing_image_is_an_error_not_a_blind_answer(self):
        tid = attachments.add_task(self.cfg, self.conn, "what is this", [PNG])
        os.remove(attachments.paths(self.cfg, db.get(self.conn, tid))[0])
        self.night(18, hour=12).run()
        self.assertIn("missing", db.get(self.conn, tid)["last_error"])

    def test_backends_attach_images(self):
        p = os.path.join(self.tmp, "1.png")
        open(p, "wb").write(PNG)
        from pathlib import Path
        api = backends.AnthropicAPI("claude_api", {"model": "m"})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(backends, "_post", return_value={"content": [{"type": "text", "text": "a cat"}]}) as post:
            self.assertEqual(api.run("describe", images=[Path(p)]).text, "a cat")
        content = post.call_args[0][2]["messages"][0]["content"]
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[-1], {"type": "text", "text": "describe"})
        with self.assertRaises(BackendError):
            backends.OpenAICompat("local", {"model": "m", "base_url": "x"}).run("x", images=[Path(p)])
        cmd = backends.Command("c", {"argv": ["cat"]})
        self.assertIn(p, cmd.run("look", images=[Path(p)]).text)

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
        body, ctype = multipart({"prompt": "make an app like this", "tier": "agent"}, [PNG])
        self.assertEqual(call("/tasks", body, {"Content-Type": ctype})[0], 401)
        bearer = {"Authorization": "Bearer s3cret-token-123456"}
        code, out, _ = call("/tasks", body, {"Content-Type": ctype, **bearer})  # a Shortcut
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(out)["images"], 1)
        bad, ctype2 = multipart({"prompt": "x"}, [b"#!/bin/sh"])
        code, out, _ = call("/tasks", bad, {"Content-Type": ctype2, **bearer})
        self.assertEqual(code, 400)
        local, ctype3 = multipart({"prompt": "x", "tier": "local"}, [PNG])
        self.assertEqual(call("/tasks", local, {"Content-Type": ctype3, **bearer})[0], 400)
        cookie = call("/login", b"token=s3cret-token-123456",
                      {"Content-Type": "application/x-www-form-urlencoded"})[2]["Set-Cookie"].split(";")[0]
        me = {"Cookie": cookie}
        code, _, h = call("/tasks", body, {"Content-Type": ctype, **me, "Origin": base})  # the form
        self.assertEqual((code, h["Location"][:12]), (303, "/?m=Queued%2"))
        empty, ctype4 = multipart({"prompt": "no picture"}, [b""])  # form sent with no file chosen
        self.assertEqual(call("/tasks", empty, {"Content-Type": ctype4, **me})[0], 303)
        page = call("/t/1", headers=me)[1].decode()
        self.assertIn("<img src=/t/1/img/1", page)
        self.assertIn("1 image", call("/", headers=me)[1].decode())
        code, img, h = call("/t/1/img/1", headers=me)
        self.assertEqual((code, img, h["Content-Type"]), (200, PNG, "image/png"))
        self.assertEqual(call("/t/1/img/1")[0], 303)  # not signed in
        self.assertEqual(call("/t/1/img/2", headers=me)[0], 404)
        self.assertEqual(call("/tasks/1/img/1", headers=bearer)[0], 200)
        self.assertEqual(json.loads(call("/tasks/1", headers=bearer)[1])["images"], ["1.png"])
        srv.shutdown()
        rows = db.list_tasks(db.connect(self.cfg["paths"]["db"]))
        self.assertEqual([(r["prompt"], r["tier"], bool(r["images"])) for r in rows],
                         [("no picture", "auto", False), ("make an app like this", "agent", True),
                          ("make an app like this", "agent", True)])


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
