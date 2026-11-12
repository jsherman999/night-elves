import json, os, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from elves import config, db, notify, policy, web
from elves.backends import BackendError, Command, Result
from elves.dispatcher import Night, passed

class Fake:
    def __init__(self, reply="ok", usd=0.0, fail=0):
        self.reply, self.usd, self.fail, self.calls = reply, usd, fail, []
    def run(self, prompt, system=None):
        self.calls.append((prompt, system))
        if self.fail:
            self.fail -= 1
            raise BackendError("boom")
        text = self.reply(prompt, system) if callable(self.reply) else self.reply
        return Result(text, 10, 20, self.usd)

def make_cfg(tmp):
    cfg = config.load("/nonexistent")
    cfg["paths"] = {"db": ":memory:", "results": tmp}
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

    def test_passed(self):
        self.assertTrue(passed("PASS\nok") and passed("\n pass"))
        self.assertFalse(passed("FAIL: nope"))

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

    def test_notify_failure_does_not_raise(self):
        self.cfg["notify"] = {"argv": ["sleep", "5"], "timeout": 0.2}
        notify.send(self.cfg, "digest")  # timeout is reported, not raised
        self.cfg["notify"] = {"argv": ["/nonexistent/notifier", "{text}"]}
        notify.send(self.cfg, "digest")
        self.assertTrue(any(f.startswith("digest-") for f in os.listdir(self.tmp)))

    def test_parse_inline(self):
        self.assertEqual(web.parse_inline("! opus: fix it"), ("fix it", 1, "frontier"))
        self.assertEqual(web.parse_inline("plain"), ("plain", 5, "auto"))

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
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(post("/sms?key=s3cret-token-123456", b"From=%2B19990000000&Body=hi", form)[0], 401)
        code, text = post("/sms?key=s3cret-token-123456", b"From=%2B15550001111&Body=%21urgent", form)
        self.assertEqual(code, 200)
        self.assertIn("Queued #2", text)
        srv.shutdown()
        rows = db.list_tasks(db.connect(self.cfg["paths"]["db"]))
        self.assertEqual([(r["source"], r["priority"]) for r in rows], [("sms", 1), ("web", 2)])

if __name__ == "__main__":
    unittest.main()
