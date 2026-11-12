import contextlib, io, json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from elves import cli, config, db, notify, policy, web, when
from elves.backends import BackendError, Command, Result
from elves.dispatcher import Night, passed

class Fake:
    def __init__(self, reply="ok", usd=0.0, fail=0):
        self.reply, self.usd, self.fail, self.calls = reply, usd, fail, []
    def run(self, prompt, system=None, cwd=None):
        self.calls.append((prompt, system))
        self.cwd = cwd
        if self.fail:
            self.fail -= 1
            raise BackendError("boom")
        text = self.reply(prompt, system) if callable(self.reply) else self.reply
        return Result(text, 10, 20, self.usd)

def make_cfg(tmp):
    cfg = config.load("/nonexistent")
    cfg["paths"] = {"db": ":memory:", "results": tmp, "work": os.path.join(tmp, "work")}
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

    def test_agent_tier_runs_in_its_workspace(self):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        tid = db.add_task(self.conn, "capture something", tier="agent")
        def work(prompt, system):
            open(os.path.join(agent.cwd, "out.json"), "w").write("{}")
            return "saved out.json"
        agent = Fake(work)
        stats = self.night(18, hour=2, claude_agent=agent).run()
        t = db.get(self.conn, tid)
        self.assertEqual((stats["done"], t["backend"]), (1, "claude_agent"))
        self.assertEqual(agent.cwd, os.path.join(self.tmp, "work", str(tid)))
        self.assertIn("unattended", agent.calls[0][1])
        self.assertIn("- out.json", open(t["result_path"]).read())
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

class MiscTests(Base):
    def test_command_backend_arg_and_stdin(self):
        self.assertEqual(Command("e", {"argv": ["echo", "{prompt}"]}).run("hi; rm -rf /").text, "hi; rm -rf /")
        self.assertEqual(Command("c", {"argv": ["cat"]}).run("via stdin", "sys").text, "sys\n\nvia stdin")
        with self.assertRaises(BackendError):
            Command("f", {"argv": ["false"]}).run("x")

    def test_command_backend_drops_api_key(self):
        sh = ["sh", "-c", 'echo "${ANTHROPIC_API_KEY:-unset} ${ELVES_KEEP:-unset}"']
        with mock.patch.dict(os.environ, ANTHROPIC_API_KEY="sk-test", ELVES_KEEP="kept"):
            self.assertEqual(Command("c", {"argv": sh}).run("x").text, "unset kept")
            self.assertEqual(Command("c", {"argv": sh, "env_unset": []}).run("x").text, "sk-test kept")

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

if __name__ == "__main__":
    unittest.main()
