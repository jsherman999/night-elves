import contextlib, html, io, json, os, shutil, subprocess, tempfile, threading, time, unittest
import urllib.error, urllib.request
from unittest import mock
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from elves import attachments, backends, cli, config, db, notify, pages, policy, web, webtools, when
from elves.backends import BackendError, Command, Result
from elves.dispatcher import Night, passed, plan_criteria

class Fake:
    def __init__(self, reply="ok", usd=0.0, fail=0):
        self.reply, self.usd, self.fail, self.calls = reply, usd, fail, []
    def run(self, prompt, system=None, cwd=None, files=(), args=(), web=False):
        self.calls.append((prompt, system))
        self.cwd, self.files, self.args, self.web = cwd, list(files), list(args), web
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
    cfg["roles"]["classifier"] = "off"  # SizeUpTests turn it on
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
            return "saved out.json\nSTATUS: DONE\nCHECK: test -f out.json"
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
        self.assertIn("`test -f out.json` exited 0", listing)
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

    def agent_cfg(self, **extra):
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5],
                                                "max_runs_per_night": 9,
                                                "session_args": ["--session-id", "{session}"],
                                                "resume_args": ["--resume", "{session}"], **extra}

    def test_agent_check_failure_is_fed_back_and_the_session_resumed(self):
        self.agent_cfg()
        tid = db.add_task(self.conn, "make out.txt say hi", tier="agent")
        def work(prompt, system):
            n = len(agent.calls)
            open(os.path.join(agent.cwd, "out.txt"), "w").write("hi" if n > 1 else "nope")
            return "made it\nSTATUS: DONE\nCHECK: `grep -qx hi out.txt`"
        agent = Fake(work)
        stats = self.night(18, hour=2, claude_agent=agent).run()
        t = db.get(self.conn, tid)
        self.assertEqual((stats["done"], t["status"], t["attempts"]), (1, "done", 1))
        self.assertEqual(len(agent.calls), 2)
        first, second = agent.calls
        self.assertIn("STATUS: DONE", first[1])  # the contract is in the system prompt
        sid = t["session"].split()[1]
        self.assertEqual(t["session"], f"claude_agent {sid}")
        self.assertEqual(agent.args, ["--resume", sid])
        self.assertIsNone(second[1])  # a resumed session already has its instructions
        self.assertIn("This continues your earlier work", second[0])
        self.assertIn("`grep -qx hi out.txt` exited 1", second[0])
        self.assertIn("exited 0", open(t["result_path"]).read())

    def test_agent_incomplete_or_silent_is_not_accepted(self):
        self.agent_cfg()
        a = db.add_task(self.conn, "a", tier="agent")
        b = db.add_task(self.conn, "b", tier="agent")
        replies = {"a": "half done\nSTATUS: INCOMPLETE\nCHECK: none", "b": "all done, trust me"}
        agent = Fake(lambda p, s: replies["a" if "Task:\na" in p else "b"])
        stats = self.night(18, hour=2, claude_agent=agent).run()
        self.assertEqual(stats["failed"], 2)
        ta, tb = db.get(self.conn, a), db.get(self.conn, b)
        self.assertEqual((ta["attempts"], tb["attempts"]), (3, 3))
        self.assertIn("INCOMPLETE", ta["feedback"])
        self.assertIn("didn't end with the STATUS", tb["feedback"])
        self.assertTrue(ta["last_error"].startswith("not accepted"))
        self.assertIn("not accepted", open(ta["result_path"]).read())  # last attempt is kept

    def test_agent_check_none_is_accepted_and_a_check_is_sandboxed(self):
        self.agent_cfg()
        a = db.add_task(self.conn, "look something up", tier="agent")
        b = db.add_task(self.conn, "sneaky", tier="agent")
        outside = os.path.join(os.path.expanduser("~"), f".elves-test-{os.getpid()}")
        agent = Fake(lambda p, s: "found it\nSTATUS: DONE\nCHECK: none" if "look" in p else
                     f"STATUS: DONE\nCHECK: touch {outside}")
        self.night(18, hour=2, claude_agent=agent).run()
        self.assertEqual(db.get(self.conn, a)["status"], "done")
        self.assertIn("CHECK: none", open(db.get(self.conn, a)["result_path"]).read())
        self.assertFalse(os.path.exists(outside))  # the check can't write outside the workspace
        self.assertIn("Operation not permitted", db.get(self.conn, b)["feedback"])

    def test_agent_gone_session_starts_fresh(self):
        self.agent_cfg()
        tid = db.add_task(self.conn, "x", tier="agent")
        db.update(self.conn, tid, session="claude_agent 1234", attempts=1, last_error="timed out")
        def work(prompt, system):
            if agent.args[0] == "--resume":
                raise BackendError("claude_agent: exit 1: No conversation found with session ID: 1234")
            return "STATUS: DONE\nCHECK: true"
        agent = Fake(work)
        self.night(18, hour=2, claude_agent=agent).run()
        t = db.get(self.conn, tid)
        self.assertEqual(t["status"], "done")
        self.assertEqual(agent.args[0], "--session-id")
        self.assertNotEqual(t["session"], "claude_agent 1234")
        self.assertIn("stopped before it finished: timed out", agent.calls[-1][0])

    def test_local_agent_goes_first_then_claude(self):
        self.agent_cfg()
        self.cfg["backends"]["local_agent"] = {"type": "command", "argv": ["x"],
                                               "session_args": ["--session", "ses_{session_hex}"],
                                               "notes": "Playwright is installed."}
        self.cfg["roles"]["local_agent"] = "local_agent"
        easy = db.add_task(self.conn, "easy", tier="agent")
        hard = db.add_task(self.conn, "hard", tier="agent", priority=2)
        la = Fake("STATUS: INCOMPLETE")  # never finishes
        claude = Fake("STATUS: DONE\nCHECK: true")
        self.night(18, hour=2, claude_agent=claude, local_agent=la).run()
        self.assertEqual(len(la.calls), 2)  # escalate_after
        self.assertIn("Playwright is installed.", la.calls[0][1])
        self.assertTrue(la.args[1].startswith("ses_") and "-" not in la.args[1])
        self.assertEqual(len(claude.calls), 2)  # the hard task, and the easy one after escalating
        self.assertTrue(db.get(self.conn, easy)["session"].startswith("claude_agent "))
        self.assertEqual([db.get(self.conn, i)["backend"] for i in (easy, hard)],
                         ["claude_agent", "claude_agent"])
        # By day, Claude's window is shut but the local agent runs.
        day = db.add_task(self.conn, "by day", tier="agent")
        self.night(18, hour=12, local_agent=Fake("STATUS: DONE\nCHECK: true")).run()
        self.assertEqual(db.get(self.conn, day)["backend"], "local_agent")

    def test_passed(self):
        self.assertTrue(passed("PASS\nok") and passed("\n pass") and passed("`PASS`"))
        self.assertTrue(passed("Looks fine to me."))  # no verdict at all: don't fail the work
        for review in ("FAIL: nope", "**FAIL**\nmissing tests", "## Verdict: FAIL",
                       "**Verdict:** FAIL", "Here is my review.\nIt FAILS the second check.",
                       "> FAIL"):
            self.assertFalse(passed(review), review)

    def test_conserve_mode_reviews_local_work_locally(self):
        tid = db.add_task(self.conn, "summarise logs")
        reviews = []
        def reply(prompt, system):
            if "reviewing" not in system:
                return "answer"
            reviews.append(prompt)
            return "FAIL\nno numbers" if len(reviews) == 1 else "PASS"
        local = Fake(reply)
        stats = self.night(5, hour=12, local=local).run()
        self.assertEqual(stats["done"], 1)
        systems = [s for _, s in local.calls]
        self.assertEqual(sum("reviewing" in s for s in systems), 2)  # rejected once, then passed
        self.assertIn("no numbers", local.calls[2][0])  # the retry carried the feedback
        t = db.get(self.conn, tid)
        self.assertEqual((t["attempts"], t["feedback"]), (1, None))  # cleared once it was done
        self.assertEqual(len(self.fakes["claude_api"].calls), 0)

    def test_local_review_role_picks_the_reviewer(self):
        self.cfg["backends"]["qwen"] = {"type": "openai_compat"}
        self.cfg["roles"]["local_review"] = "qwen"
        db.add_task(self.conn, "summarise logs")
        qwen = Fake("PASS")
        self.night(5, hour=12, qwen=qwen).run()
        self.assertEqual(len(qwen.calls), 1)
        self.assertIn("reviewing", qwen.calls[0][1])
        self.assertEqual(len(self.fakes["local"].calls), 1)

    def test_criteria_reach_the_executor_and_reviewer(self):
        db.add_task(self.conn, "write a script", criteria="it prints the total")
        self.night(5, hour=12).run()
        execute, review = self.fakes["local"].calls
        self.assertIn("done only when:\nit prints the total", execute[0])
        self.assertIn("Done when:\nit prints the total", review[0])

    def test_planner_writes_criteria_when_there_are_none(self):
        self.assertEqual(plan_criteria("1. a\n2. b\n\n**Done when:**\n- it runs\n- it's fast"),
                         "- it runs\n- it's fast")
        self.assertEqual(plan_criteria("## Done when: tests pass"), "tests pass")
        self.assertEqual(plan_criteria("1. just a plan"), "")
        db.add_task(self.conn, "write a script")
        api = Fake(lambda p, s: "1. do it\nDone when:\n- exits 0" if "planning" in s else "PASS",
                   usd=0.1)
        self.night(18, hour=12, claude_api=api).run()
        self.assertIn("Done when:\n- exits 0\n\nResult:", api.calls[1][0])

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
            cli.main(["-c", path, "add", "--done-when", " the tests pass ", "other"])
            self.assertEqual(cli.main(["-c", path, "edit", "1", "-t", "agent", "-p", "1",
                                       "--prompt", "new words", "--tonight",
                                       "--done-when", "a screenshot exists"]), 0)
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
        self.assertEqual((one["criteria"], two["criteria"]), ("a screenshot exists", "the tests pass"))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["-c", path, "edit", "2", "--done-when", ""])
        self.assertIsNone(db.get(conn, 2)["criteria"])
        self.assertIn("no task #7", err.getvalue())
        self.assertIn("cancelled #2", out.getvalue())

class MiscTests(Base):
    def test_command_backend_arg_and_stdin(self):
        self.assertEqual(Command("e", {"argv": ["echo", "{prompt}"]}).run("hi; rm -rf /").text, "hi; rm -rf /")
        self.assertEqual(Command("c", {"argv": ["cat"]}).run("via stdin", "sys").text, "sys\n\nvia stdin")
        with self.assertRaises(BackendError):
            Command("f", {"argv": ["false"]}).run("x")
        with self.assertRaisesRegex(BackendError, "exit 1: Error: Reached max turns"):
            Command("m", {"argv": ["sh", "-c", "echo 'Error: Reached max turns (200)'; exit 1"]}).run("x")
        self.assertEqual(Command("a", {"argv": ["echo"]}).run("x", args=["--session", "s"]).text,
                         "--session s")  # prompt on stdin, args after argv

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

    def test_contract_parsing(self):
        from elves import verify
        self.assertEqual(verify.contract("did it\n\n**STATUS:** DONE\nCHECK: `npm test`"),
                         ("DONE", "npm test"))
        self.assertEqual(verify.contract("STATUS: INCOMPLETE\nCHECK: none."), ("INCOMPLETE", ""))
        self.assertEqual(verify.contract("- STATUS: done\n- CHECK: sh check.sh"), ("DONE", "sh check.sh"))
        self.assertEqual(verify.contract("just a summary"), (None, None))
        self.assertEqual(verify.contract("STATUS: DONE"), ("DONE", None))

    def test_check_sandbox(self):
        from elves import verify
        if not os.path.exists(verify.SANDBOX):
            self.skipTest("needs macOS sandbox-exec")
        work = os.path.join(self.tmp, "w")
        os.makedirs(work)
        secret = os.path.join(self.tmp, "secret")
        open(secret, "w").write("s3cret")
        def run(cmd, timeout=20):
            return verify.run_check(cmd, work, dict(os.environ), timeout, [secret])
        self.assertEqual(run("echo hi > f && cat f"), (0, "hi"))
        code, out = run(f"cat {secret}")
        self.assertNotEqual(code, 0)
        self.assertNotIn("s3cret", out)
        outside = os.path.join(os.path.expanduser("~"), f".elves-test-{os.getpid()}")
        self.assertNotEqual(run(f"touch {outside}")[0], 0)  # (temp folders are writable)
        self.assertFalse(os.path.exists(outside))
        self.assertNotEqual(run("sh -c 'sudo -n true'")[0], 0)
        self.assertNotEqual(run("curl -s -m 5 https://example.com")[0], 0)
        code, out = run("python3 -c \"import socket,threading;s=socket.socket();s.bind(('127.0.0.1',0));"
                        "s.listen();socket.create_connection(s.getsockname()).close();print('local ok')\"")
        self.assertEqual((code, out), (0, "local ok"))
        mark = os.path.join(work, "mark")
        start = time.monotonic()
        self.assertEqual(run(f"(sleep 2; touch {mark}) & sleep 30", timeout=0.5)[0], None)
        self.assertLess(time.monotonic() - start, 10)
        time.sleep(2.5)
        self.assertFalse(os.path.exists(mark))  # what it left running was killed

    def test_notify_failure_does_not_raise(self):
        self.cfg["notify"] = {"argv": ["sleep", "5"], "timeout": 0.2}
        notify.send(self.cfg, "digest")  # timeout is reported, not raised
        self.cfg["notify"] = {"argv": ["/nonexistent/notifier", "{text}"]}
        notify.send(self.cfg, "digest")
        self.assertTrue(any(f.startswith("digest-") for f in os.listdir(self.tmp)))

    def test_parse_inline(self):
        self.assertEqual(web.parse_inline("! opus: fix it"), ("fix it", 1, "frontier", None, []))
        self.assertEqual(web.parse_inline("plain"), ("plain", 5, "auto", None, []))
        self.assertEqual(web.parse_inline("Agent: fetch it"), ("fetch it", 5, "agent", None, []))
        self.assertEqual(web.parse_inline("at 1:30am agent: capture"), ("capture", 5, "agent", "1:30am", []))
        self.assertEqual(web.parse_inline("agent: At 2 PM CST, scan it"), ("scan it", 5, "agent", "2 PM", []))
        self.assertEqual(web.parse_inline("! at 01:30: do it"), ("do it", 1, "auto", "01:30", []))
        self.assertEqual(web.parse_inline("At 5 companies, compare"), ("At 5 companies, compare", 5, "auto", None, []))
        self.assertEqual(web.parse_inline("at 1:30 make tea"), ("make tea", 5, "auto", "1:30", []))
        self.assertEqual(web.parse_inline("from #9: build it"), ("build it", 5, "auto", None, [9]))
        self.assertEqual(web.parse_inline("agent: from #9, #12 and #9 build"),
                         ("build", 5, "agent", None, [9, 12]))
        self.assertEqual(web.parse_inline("From the top, again"), ("From the top, again", 5, "auto", None, []))

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
        code, _, h = call("/tasks", b"prompt=agent%3A+from+the+page&priority=5&at=&criteria=it+runs",
                          {**form, **me})
        self.assertEqual((code, h["Location"]), (303, "/?m=Queued%20%234"))
        self.assertEqual((db.get(conn, 4)["tier"], db.get(conn, 4)["criteria"]), ("agent", "it runs"))
        self.assertIn("<b>Done when:</b> it runs", call("/t/4", headers=me)[1])
        # images the agent saved in its workspace show on the task's page, and nothing else does
        work = os.path.join(self.cfg["paths"]["work"], "4")
        for rel in ("shots/sign in.png", ".cache/hidden.png", "node_modules/x.png"):
            os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
            open(os.path.join(work, rel), "wb").write(PNG)
        open(os.path.join(work, "page.svg"), "w").write("<svg onload=alert(1)>")
        os.symlink("/etc/hosts", os.path.join(work, "hosts.png"))
        page = call("/t/4", headers=me)[1]
        self.assertIn("src=/t/4/w/shots/sign%20in.png", page)
        self.assertNotIn("hidden.png", page)
        self.assertNotIn("node_modules", page)
        code, body, h = call("/t/4/w/shots/sign%20in.png", headers=me)
        self.assertEqual((code, h["Content-Type"]), (200, "image/png"))
        for bad in ("page.svg", "hosts.png", ".cache/hidden.png", "..%2F..%2Fq.db", "%2Fetc%2Fhosts"):
            self.assertEqual(call(f"/t/4/w/{bad}", headers=me)[0], 404, bad)
        self.assertEqual(call("/t/4/w/shots/sign%20in.png")[0], 303)  # not signed in
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
            return "done\nSTATUS: DONE\nCHECK: none"
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

    def test_results_of_earlier_tasks_can_be_attached(self):
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        conn = db.connect(self.cfg["paths"]["db"])
        nine = db.add_task(conn, "3B2 notes")
        res = os.path.join(self.tmp, f"{nine}.md")
        open(res, "w").write("# Task 1\n\nThe 3B2/400 ran System V.")
        db.update(conn, nine, status="done", result_path=res)
        pending = db.add_task(conn, "not done yet")
        outside = db.add_task(conn, "elsewhere")
        db.update(conn, outside, status="done", result_path="/etc/hosts")
        self.assertEqual(attachments.parse_refs(["9,#12", " 12 ", 3]), [9, 12, 3])
        with self.assertRaises(ValueError):
            attachments.parse_refs("nine")
        # CLI
        with mock.patch.object(config, "load", return_value=self.cfg), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["add", "--from", str(nine), "build an emulator"]), 0)
            self.assertEqual(cli.main(["add", "--from", str(pending), "x"]), 2)
            self.assertEqual(cli.main(["add", "--from", str(outside), "x"]), 2)
            self.assertEqual(cli.main(["add", "--from", "99", "x"]), 2)
        self.assertIn("with 1 file", out.getvalue())
        self.assertIn(f"#{pending} has no result yet", err.getvalue())
        self.assertIn(f"#{outside} has no result yet", err.getvalue())  # not under results/
        self.assertIn("no task #99", err.getvalue())
        t = db.list_tasks(conn)[0]
        self.assertEqual(attachments.files(t)[0], {"name": "1-task-1.md", "kind": "text",
                                                   "size": os.path.getsize(res)})
        # The model gets it pasted in, like any text file.
        night = self.night(5, hour=12)
        night.conn = conn
        night.run()
        built = [c for c, _ in self.fakes["local"].calls if "build an emulator" in c][0]
        self.assertIn("Attached file 1-task-1.md:\n```\n# Task 1\n\nThe 3B2/400 ran System V.", built)
        # Web (JSON, a form with an upload, shorthand) and SMS
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
        auth = {"Authorization": "Bearer s3cret-token-123456"}
        js = {**auth, "Content-Type": "application/json"}
        code, text = post("/tasks", json.dumps({"prompt": "use it", "from": "1"}).encode(), js)
        self.assertEqual((code, json.loads(text)["files"]), (201, 0))
        self.assertTrue(json.loads(text)["message"].endswith("with the result of #1"))
        code, text = post("/tasks", json.dumps({"prompt": "from #1: and this"}).encode(), js)
        self.assertEqual(code, 201)
        waiting = db.add_task(conn, "still waiting")  # the run above finished `pending`
        code, text = post("/tasks", json.dumps({"prompt": "x", "from": [waiting]}).encode(), js)
        self.assertEqual((code, json.loads(text)["message"]), (400, f"Not queued: #{waiting} has no result yet"))
        body, ctype = multipart({"prompt": "both", "from": "1"}, [("notes.txt", b"mine")])
        code, text = post("/tasks", body, {**auth, "Content-Type": ctype})
        self.assertEqual(json.loads(text)["message"].split(" with ")[1], "1 file and the result of #1")
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        code, text = post("/sms?key=s3cret-token-123456",
                          b"From=%2B15550001111&Body=from+%231%3A+summarise", form)
        self.assertIn("with the result of #1", text)
        code, text = post("/sms?key=s3cret-token-123456",
                          b"From=%2B15550001111&Body=from+%2399%3A+x", form)
        self.assertIn("Not queued: no task #99", text)
        srv.shutdown()
        rows = db.list_tasks(conn, limit=6)
        self.assertEqual([r["prompt"] for r in rows], ["summarise", "both", "still waiting",
                                                       "and this", "use it", "build an emulator"])
        self.assertEqual([f["name"] for f in attachments.files(rows[1])], ["1-notes.txt", "2-task-1.md"])
        self.assertEqual(rows[0]["source"], "sms")

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

class SizeUpAndUsageTests(Base):
    def test_auto_tasks_are_sized_up_once(self):
        self.cfg["roles"]["classifier"] = ""  # the local model
        self.cfg["backends"]["claude_agent"] = {"type": "command", "argv": ["x"], "window": [1, 5]}
        build = db.add_task(self.conn, "clone github.com/x/y and add a dark mode")
        news = db.add_task(self.conn, "find reviews of the new show")
        poem = db.add_task(self.conn, "write a haiku")
        verdicts = {"clone": "AGENT", "reviews": "**WEB**", "haiku": "ANSWER, clearly"}
        def local(prompt, system):
            if system and system.startswith("Sort a task"):
                return next(v for k, v in verdicts.items() if k in prompt)
            return "local answer"
        agent = Fake("built it\nSTATUS: DONE\nCHECK: none")
        self.night(18, hour=2, local=Fake(local), claude_agent=agent).run()
        b, n, p = (db.get(self.conn, i) for i in (build, news, poem))
        self.assertEqual((b["tier"], b["needs"], b["backend"]), ("agent", "agent", "claude_agent"))
        self.assertEqual((n["tier"], n["needs"], n["backend"]), ("auto", "web", "claude_api"))
        self.assertEqual((p["needs"], p["backend"]), ("answer", "local"))
        sorts = [c for c in self.fakes["local"].calls if c[1].startswith("Sort a task")]
        self.assertEqual(len(sorts), 3)
        db.change(self.conn, poem, ("done",), prompt="write two haiku")  # an edit sizes it up again
        self.assertIsNone(db.get(self.conn, poem)["needs"])

    def test_web_tasks_wait_for_claude_rather_than_go_local(self):
        tid = db.add_task(self.conn, "latest news")
        db.update(self.conn, tid, needs="web")
        self.cfg["budget"]["conserve_night_cap_usd"] = 0.0  # no API allowance tonight
        self.cfg["budget"]["per_night_cap_usd"] = 0.0
        self.cfg["backends"]["claude_cli"]["window"] = [1, 2]
        stats = self.night(18, hour=3).run()
        self.assertEqual((stats["skipped"], db.get(self.conn, tid)["status"]), (1, "queued"))

    def test_time_tokens_and_cost_are_recorded_and_shown(self):
        tid = db.add_task(self.conn, "x", tier="frontier")
        self.night(18, hour=2).run()
        t = db.get(self.conn, tid)
        self.assertIsNotNone(t["secs"])
        u = db.usage(self.conn, [tid])[tid]
        self.assertEqual((u["in"], u["out"], u["usd"], u["est"]), (10, 20, 0.5, 0.5))
        html_ = pages.task(t, "done", usage=u)
        self.assertIn("<b>Tokens</b> 30 (10 in, 20 out)", html_)
        self.assertIn("$0.50 API credit", html_)
        self.assertIn("30 tok · $0.50", pages.row(t, u))
        db.record_spend(self.conn, "claude_agent", tid, "execute", 1000, 50, 0.0, est_usd=1.25)
        u = db.usage(self.conn, [tid])[tid]
        self.assertIn("≈$1.25 at API prices", pages.cost(u))
        self.assertEqual(pages.cost({"usd": 0.0, "est": 0.0}), "$0 (local)")
        self.assertEqual((pages.duration(59), pages.duration(754), pages.duration(4000)),
                         ("59s", "12m 34s", "1h 06m"))

    def test_claude_and_opencode_json_output(self):
        r = backends.parse_output("claude-json", 'noise\n' + json.dumps(
            {"result": "pong\nSTATUS: DONE", "total_cost_usd": 0.11, "usage": {
                "input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
                "output_tokens": 4}}))
        self.assertEqual((r.text, r.tokens_in, r.tokens_out, r.usd, r.est_usd),
                         ("pong\nSTATUS: DONE", 152, 4, 0.0, 0.11))
        lines = [{"type": "text", "part": {"text": "\n\nhello"}},
                 {"type": "step_finish", "part": {"cost": 0, "tokens": {"input": 70, "output": 4,
                                                  "reasoning": 6, "cache": {"read": 10, "write": 0}}}},
                 {"type": "text", "part": {"text": "STATUS: DONE"}},
                 {"type": "step_finish", "part": {"tokens": {"input": 90, "output": 1}}}]
        r = backends.parse_output("opencode-json", "\n".join(json.dumps(x) for x in lines))
        self.assertEqual((r.text, r.tokens_in, r.tokens_out), ("hello\n\nSTATUS: DONE", 170, 11))
        self.assertEqual(backends.parse_output("text", " plain \n").text, "plain")

    def test_api_continues_paused_turns_and_explains_an_empty_reply(self):
        api = backends.AnthropicAPI("claude_api", {"model": "m", "price_in": 4.0, "price_out": 20.0,
                                                  "server_tools": ["web_search", "web_fetch"],
                                                  "effort": "high"})
        paused = {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s"}],
                  "usage": {"input_tokens": 100, "output_tokens": 10,
                            "server_tool_use": {"web_search_requests": 2}}}
        final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}],
                 "usage": {"input_tokens": 200, "output_tokens": 20}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(backends, "_post", side_effect=[paused, final]) as post:
            res = api.run("q")
        self.assertEqual((res.text, res.tokens_in, res.tokens_out), ("answer", 300, 30))
        self.assertAlmostEqual(res.usd, (300 * 4 + 30 * 20) / 1e6 + 0.02)
        body = post.call_args_list[1].args[2]
        self.assertEqual([t["name"] for t in body["tools"]], ["web_search", "web_fetch"])
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["max_tokens"], 32000)
        self.assertEqual(body["messages"][1]["role"], "assistant")  # the paused turn went back
        thought = {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": ""}],
                   "usage": {"input_tokens": 50, "output_tokens": 32000}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(backends, "_post", return_value=thought):
            with self.assertRaises(backends.SpentError) as cm:
                api.run("q")
        self.assertIn("used all 32000 output tokens", str(cm.exception))
        self.assertEqual(cm.exception.result.tokens_out, 32000)

    def test_a_failed_billed_call_is_still_recorded(self):
        tid = db.add_task(self.conn, "x", tier="frontier")
        class Spent:
            def run(self, *a, **k):
                raise backends.SpentError("claude_api: used all", Result("", 5, 9, 0.25))
        self.night(18, hour=2, claude_api=Spent()).run()
        u = db.usage(self.conn, [tid])[tid]
        self.assertEqual((u["in"], u["out"], u["usd"]), (5, 9, 0.25))

class SearchAndArchiveTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg["paths"]["db"] = os.path.join(self.tmp, "q.db")
        self.conn = conn = db.connect(self.cfg["paths"]["db"])
        self.notes = db.add_task(conn, "Research the 3B2")
        res = os.path.join(self.tmp, f"{self.notes}.md")
        open(res, "w").write("# Notes\n\nThe 3B2/400 ran System V on a WE 32100 <b>CPU</b>.")
        db.update(conn, self.notes, status="done", result_path=res)
        self.other = db.add_task(conn, "Summarise the weather")
        db.update(conn, self.other, status="done", result_path="/etc/hosts")  # outside results
        self.waiting = db.add_task(conn, "still waiting, system unknown")

    def test_search_matches_prompts_and_results(self):
        conn, found = self.conn, lambda q: [(t["id"], s) for t, s in db.search(self.conn, q, self.tmp)]
        self.assertEqual(found("  "), [])
        self.assertEqual(found("nothing-like-this"), [])
        (tid, snippet), = found("we SYSTEM v")  # every word, any case, from the result
        self.assertEqual(tid, self.notes)
        self.assertIn("ran System V on a WE 32100", snippet)
        self.assertEqual(found("weather"), [(self.other, "")])  # the prompt alone
        self.assertEqual([i for i, _ in found("system")], [self.waiting, self.notes])
        self.assertEqual(found("localhost"), [])  # a result path outside the folder isn't read
        db.archive(conn, self.notes)
        self.assertEqual([i for i, _ in found("32100")], [self.notes])  # archived still found

    def test_archive_rules(self):
        conn = self.conn
        with self.assertRaises(ValueError):
            db.archive(conn, self.waiting)
        with self.assertRaises(LookupError):
            db.archive(conn, 99)
        before = db.get(conn, self.notes)["updated_at"]
        db.archive(conn, self.notes)
        t = db.get(conn, self.notes)
        self.assertEqual((t["archived"], t["updated_at"], t["status"]), (1, before, "done"))
        self.assertTrue(os.path.isfile(t["result_path"]))
        self.assertEqual([r["id"] for r in db.list_tasks(conn, archived=True)], [self.notes])
        self.assertNotIn(self.notes, [r["id"] for r in db.list_tasks(conn, archived=False)])
        db.archive(conn, self.notes, False)
        self.assertEqual(db.get(conn, self.notes)["archived"], 0)
        db.archive(conn, self.notes)
        db.requeue(conn, self.notes)  # running it again brings it back
        self.assertEqual(db.get(conn, self.notes)["archived"], 0)

    def test_old_database_gains_the_column(self):
        path = os.path.join(self.tmp, "old.db")
        import sqlite3
        old = sqlite3.connect(path)
        lines = [l for l in db.SCHEMA.splitlines() if "archived" not in l]
        self.assertEqual(len(lines), len(db.SCHEMA.splitlines()) - 1)
        old.executescript("\n".join(lines)
                          .replace("secs REAL,   ", "secs REAL    ", 1))
        old.execute("INSERT INTO tasks (created_at, updated_at, prompt) VALUES ('a', 'a', 'x')")
        old.commit()
        old.close()
        self.assertEqual(db.get(db.connect(path), 1)["archived"], 0)

    def test_cli(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(config, "load", return_value=self.cfg), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return cli.main(list(argv)), out.getvalue(), err.getvalue()
        code, out, _ = run("search", "system", "v")
        self.assertEqual(code, 0)
        self.assertIn(f"#{self.notes} ", out)
        self.assertIn("ran System V", out)
        self.assertEqual(run("search", "zzz")[0], 1)
        self.assertEqual(run("archive", str(self.notes))[:2], (0, f"archived #{self.notes}\n"))
        self.assertIn("(archived) Research", run("list")[1])
        self.assertIn("(archived)", run("search", "32100")[1])
        code, out, _ = run("add", "--from", str(self.notes), "build on it")  # still referable
        self.assertEqual((code, out.strip().endswith("with 1 file(s)")), (0, True))
        self.assertEqual(run("archive", str(self.waiting))[0], 1)
        self.assertEqual(run("unarchive", str(self.notes))[0], 0)
        self.assertEqual(db.get(self.conn, self.notes)["archived"], 0)

    def test_web(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.cfg, "s3cret-token-123456"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        def call(path, data=None, headers=None):
            req = urllib.request.Request(base + path, data=data, headers=headers or {})
            try:
                with opener.open(req) as r:
                    return r.status, r.read().decode(), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(), e.headers
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        me = {"Cookie": call("/login", b"token=s3cret-token-123456", form)[2]["Set-Cookie"].split(";")[0]}
        n = self.notes
        home = call("/", headers=me)[1]
        self.assertIn(f"aria-label='Archive #{n}'", home)
        self.assertNotIn(f"aria-label='Archive #{self.waiting}'", home)  # not finished
        code, _, h = call(f"/tasks/{n}", b"action=archive&back=home", {**form, **me, "Origin": base})
        self.assertEqual((code, h["Location"][:13]), (303, "/?m=Archived%"))
        home = call("/", headers=me)[1]
        self.assertNotIn("Research the 3B2", home)
        self.assertIn("Summarise the weather", home)
        old = call("/?archived=1", headers=me)[1]
        self.assertIn("Research the 3B2", old)
        self.assertIn(f"aria-label='Unarchive #{n}'", old)
        hit = call("/?q=system+v", headers=me)[1]  # archived, and still found
        self.assertIn("Matches for “system v”", hit)
        self.assertIn("ran System V on a WE 32100 &lt;b&gt;CPU&lt;/b&gt;", hit)
        self.assertIn("<span class='pill cancelled'>archived</span>", hit)
        self.assertIn("<input name=from id=from ", hit)
        self.assertIn(f"aria-label='Use the result of #{n}'", hit)
        self.assertIn(f"v.push(&#x27;{n}&#x27;)", hit)
        both = call("/?q=system", headers=me)[1]  # a task with no result can't be used
        self.assertNotIn(f"Use the result of #{self.waiting}", both)
        self.assertIn('value="&lt;i&gt;"', call("/?q=%3Ci%3E", headers=me)[1])
        self.assertIn("No task", call("/?q=zzz", headers=me)[1])
        page = call(f"/t/{n}", headers=me)[1]
        self.assertIn("value=unarchive", page)
        self.assertIn("The 3B2/400 ran", page)  # the result is still there
        code, _, h = call(f"/tasks/{n}", b"action=unarchive&back=archived", {**form, **me, "Origin": base})
        self.assertEqual((code, h["Location"][:15]), (303, "/?archived=1&m="))
        self.assertIn("Research the 3B2", call("/", headers=me)[1])
        # JSON
        js = {"Authorization": "Bearer s3cret-token-123456", "Content-Type": "application/json"}
        code, out, _ = call(f"/tasks/{n}", b'{"action": "archive"}', js)
        self.assertEqual((code, json.loads(out)["task"]["archived"]), (200, 1))
        code, out, _ = call(f"/tasks/{self.waiting}", b'{"action": "archive"}', js)
        self.assertEqual(code, 400)
        self.assertEqual(call("/tasks?q=32100")[0], 401)
        code, out, _ = call("/tasks?q=32100", headers=js)
        got = json.loads(out)
        self.assertEqual([(t["id"], t["archived"]) for t in got], [(n, 1)])
        self.assertIn("WE 32100", got[0]["snippet"])
        code, out, _ = call("/tasks", json.dumps({"prompt": "use it", "from": n}).encode(), js)
        self.assertEqual(code, 201)  # an archived result can still be attached

class LocalSearchTests(Base):
    def serve(self, routes):
        from http.server import BaseHTTPRequestHandler
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code, ctype, body, *extra = routes.get(self.path.split("?")[0], (404, "text/plain", b"no"))
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                H.seen.append(self.path)
            def log_message(self, *a):
                pass
        H.seen = []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}", H.seen

    def test_only_public_http_urls_are_fetched(self):
        for bad in ("file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1:8377/",
                    "http://localhost:1234/v1/models", "http://192.168.4.1/", "http://[::1]/",
                    "http://169.254.169.254/latest", "http://10.0.0.5/", "notaurl"):
            with self.assertRaises(webtools.Refused, msg=bad):
                webtools.check_url(bad)
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(webtools.socket, "getaddrinfo", return_value=public):
            self.assertEqual(webtools.check_url("https://example.com/a?b=1"), "https://example.com/a?b=1")
        mixed = public + [(2, 1, 6, "", ("10.0.0.5", 443))]  # one private address is enough
        with mock.patch.object(webtools.socket, "getaddrinfo", return_value=mixed):
            with self.assertRaises(webtools.Refused):
                webtools.check_url("https://rebind.example/")
        s = webtools.Session("http://127.0.0.1:1")
        self.assertIn("not on the public internet", s.call("fetch_page", '{"url": "http://127.0.0.1:8377/"}'))
        self.assertEqual(s.opened, [])
        with mock.patch.object(webtools, "check_url", side_effect=webtools.Refused("no")) as chk:
            with self.assertRaises(webtools.Refused):  # each redirect is checked too
                webtools._Redirects().redirect_request(None, None, 302, "", {}, "http://10.0.0.5/")
        chk.assert_called_once_with("http://10.0.0.5/")

    def test_html_becomes_text_with_links(self):
        text = webtools.html_text(
            "<html><head><title>T</title><style>p{}</style><script>evil()</script></head>"
            "<body><h1>Files</h1><p>Get <a href='/pub/a.tar'>the  archive</a> &amp; more"
            "<a href='javascript:x()'>js</a></p><table><tr><td>a</td><td>b</td></tr></table>",
            "https://ex.org/dir/")
        self.assertIn("[the archive](https://ex.org/pub/a.tar)", text)
        self.assertIn("& more", text)
        self.assertIn("a | b", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("p{}", text)
        self.assertNotIn("javascript:", text)

    def test_search_fetch_paging_and_errors(self):
        hits = {"results": [{"title": "3B2  notes", "url": "https://ex.org/3b2", "content": "WE 32100"},
                            {"title": "Other", "url": "https://ex.org/o", "content": ""}]}
        base, seen = self.serve({
            "/search": (200, "application/json", json.dumps(hits).encode()),
            "/long": (200, "text/html; charset=utf-8", b"<p>" + b"abcdefghij" * 300 + b"</p>"),
            "/bin": (200, "application/zip", b"PK"),
            "/hop": (302, "text/plain", b"", ("Location", "/long")),
        })
        s = webtools.Session(base, page_chars=1000)
        out = s.call("web_search", '{"query": "3b2 cpu"}')
        self.assertEqual(out, "1. 3B2 notes\n   https://ex.org/3b2\n   WE 32100\n2. Other\n   https://ex.org/o")
        self.assertIn("q=3b2+cpu&format=json", seen[0])
        self.assertIn("not on the public internet", s.call("fetch_page", {"url": base + "/long"}))
        with mock.patch.object(webtools, "ALLOW_PRIVATE", True):
            first = s.call("fetch_page", json.dumps({"url": base + "/long"}))
            self.assertTrue(first.startswith("abcdefghij"))
            self.assertIn("[Characters 0-1000 of 3000. To read on: fetch_page with start=1000.]", first)
            last = s.call("fetch_page", {"url": base + "/long", "start": 2000})
            self.assertEqual(last, "abcdefghij" * 100)  # the end: no note
            self.assertEqual(seen.count("/long"), 1)  # reading on doesn't fetch again
            self.assertIn("HTTP 404", s.call("fetch_page", {"url": base + "/gone"}))
            self.assertIn("application/zip file of 2 bytes", s.call("fetch_page", {"url": base + "/bin"}))
            self.assertTrue(s.call("fetch_page", {"url": base + "/hop"}).startswith("abcdefghij"))
        self.assertIn("no tool named", s.call("rm", "{}"))
        self.assertTrue(s.call("web_search", "not json").startswith("Error:"))
        self.assertEqual(s.searches, ["3b2 cpu"])
        self.assertEqual(s.opened, [base + "/long", base + "/hop"])
        self.assertIn(f"ran 1 web search and opened 2 pages:*\n- <{base}/long>", s.note())
        self.assertIn("didn't, so this answer is from its memory", webtools.Session(base).note())

    def test_backend_runs_the_tool_loop(self):
        def reply(content=None, calls=()):
            m = {"content": content}
            if calls:
                m["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                    "function": {"name": n, "arguments": json.dumps(a)}}
                                   for i, (n, a) in enumerate(calls)]
            return {"choices": [{"message": m}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
        cfgb = {"model": "m", "base_url": "http://x/v1", "search_url": "http://s", "max_tool_uses": 3}
        b = backends.OpenAICompat("local", cfgb)
        ran = []
        def tool(self, name, arguments):
            ran.append((name, json.loads(arguments)))
            self.opened.append("https://ex.org/3b2")
            return f"result of {name}"
        bodies = []
        def post(url, headers, body, timeout):
            bodies.append(json.loads(json.dumps(body)))  # a copy: the list grows afterwards
            return replies.pop(0)
        replies = [reply(calls=[("web_search", {"query": "3b2"})]),
                   reply(calls=[("fetch_page", {"url": "https://ex.org/3b2"})]),
                   reply("The 3B2 used the WE 32100.")]
        with mock.patch.object(backends, "_post", side_effect=post), \
                mock.patch.object(webtools.Session, "call", tool):
            res = b.run("which cpu?", "Be brief.", web=True)
        self.assertTrue(res.text.startswith("The 3B2 used the WE 32100.\n\n---\n*night-elves:"))
        self.assertIn("- <https://ex.org/3b2>", res.text)
        self.assertEqual((res.tokens_in, res.tokens_out), (15, 21))  # every turn counts
        self.assertEqual(ran, [("web_search", {"query": "3b2"}), ("fetch_page", {"url": "https://ex.org/3b2"})])
        self.assertEqual([t["function"]["name"] for t in bodies[0]["tools"]], ["web_search", "fetch_page"])
        self.assertTrue(bodies[0]["messages"][0]["content"].startswith("Be brief.\n\nYou can search"))
        self.assertEqual([m["role"] for m in bodies[2]["messages"]],
                         ["system", "user", "assistant", "tool", "assistant", "tool"])
        self.assertEqual(bodies[2]["messages"][3], {"role": "tool", "tool_call_id": "c0",
                                                    "content": "result of web_search"})
        # The budget: calls past it aren't run, and the model is made to answer.
        ran.clear(); bodies.clear()
        replies = [reply(calls=[("web_search", {"query": str(i)}) for i in range(4)]),
                   reply("best I can do")]
        with mock.patch.object(backends, "_post", side_effect=post), \
                mock.patch.object(webtools.Session, "call", tool):
            res = b.run("q", web=True)
        self.assertEqual(len(ran), 3)
        self.assertEqual(bodies[1]["messages"][-2]["content"], "Not run: the tool budget for this task is used up.")
        self.assertEqual(bodies[1]["tool_choice"], "none")
        self.assertTrue(res.text.startswith("best I can do"))
        # Without web=True, or without a search_url, it is the plain single call.
        for backend, kw in ((b, {}), (backends.OpenAICompat("l", {"model": "m", "base_url": "http://x/v1"}),
                                      {"web": True})):
            bodies.clear()
            replies = [reply("plain")]
            with mock.patch.object(backends, "_post", side_effect=post):
                self.assertEqual(backend.run("q", **kw).text, "plain")
            self.assertNotIn("tools", bodies[0])

    def test_tasks_reach_a_local_model_that_can_search(self):
        self.cfg["backends"]["local"]["search_url"] = "http://127.0.0.1:8888"
        self.assertTrue(policy.local_search(self.cfg, "local"))
        self.assertFalse(policy.local_search(self.cfg, "claude_api"))
        self.assertFalse(policy.local_search(self.cfg, None))
        web_task = db.add_task(self.conn, "latest news")
        db.update(self.conn, web_task, needs="web")
        night = self.night(3)  # conserve mode, API allowance left: Claude still goes first
        night.run()
        self.assertEqual(db.get(self.conn, web_task)["backend"], "claude_api")
        self.cfg["budget"]["conserve_night_cap_usd"] = 0.0  # none left, and outside the CLI's hours
        self.cfg["backends"]["claude_cli"]["window"] = [1, 2]
        later = db.add_task(self.conn, "more news")
        db.update(self.conn, later, needs="web")
        plain = db.add_task(self.conn, "explain TCP", tier="local")
        night = self.night(3, hour=9)
        night.run()
        self.assertEqual([db.get(self.conn, t)["backend"] for t in (later, plain)], ["local", "local"])
        self.assertTrue(self.fakes["local"].web)  # the executor was told it may search

class PushTests(Base):
    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                              capture_output=True, text=True, check=True).stdout.strip()

    def setUp(self):
        super().setUp()
        from elves import push
        self.push = push
        self.remote = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.tmp, "seed")
        self.git("clone", "-q", self.remote, seed)
        open(os.path.join(seed, "a.txt"), "w").write("1\n")
        self.git("add", "a.txt", cwd=seed)
        self.git("commit", "-qm", "one", cwd=seed)
        self.git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.work = os.path.join(self.tmp, "work", "1")
        os.makedirs(self.work)
        self.clone = os.path.join(self.work, "app")
        self.git("clone", "-q", self.remote, self.clone)
        self.git("remote", "set-url", "origin", "https://github.com/jsherman999/app.git", cwd=self.clone)
        open(os.path.join(self.clone, "a.txt"), "w").write("2\n")
        self.git("commit", "-qam", "two", cwd=self.clone)
        self.cfg["push"]["owners"] = ["jsherman999"]

    def run_push(self, prompt="change it, commit and push", answer="PUSH: app main\nSTATUS: DONE"):
        from pathlib import Path
        with mock.patch.object(self.push, "DEST", self.remote):  # instead of GitHub
            return self.push.run(self.cfg, Path(self.work), prompt, answer, dict(os.environ))

    def test_a_requested_push_goes_from_the_host(self):
        note = self.run_push()
        self.assertIn("Pushed `main` to jsherman999/app", note)
        self.assertEqual(self.git("--git-dir", self.remote, "log", "-1", "--format=%s", "main"), "two")

    def test_hooks_in_the_agents_clone_never_run_on_the_host(self):
        hook = os.path.join(self.clone, ".git", "hooks", "pre-push")
        flag = os.path.join(self.tmp, "hook-ran")
        open(hook, "w").write(f"#!/bin/sh\ntouch {flag}\n")
        os.chmod(hook, 0o755)
        self.git("config", "core.fsmonitor", f"touch {flag}", cwd=self.clone)
        self.assertIn("Pushed", self.run_push())
        self.assertFalse(os.path.exists(flag))

    def test_pushes_that_are_not_allowed(self):
        self.assertIn("didn't, so nothing", self.run_push(prompt="change it"))
        self.assertIn("no PUSH line", self.run_push(answer="STATUS: DONE"))
        self.assertIn("outside the workspace", self.run_push(answer="PUSH: ../.. main"))
        self.assertIn("not a valid branch", self.run_push(answer="PUSH: app ..bad"))
        self.cfg["push"]["owners"] = ["someone-else"]
        self.assertIn("not under an owner", self.run_push())
        self.cfg["push"]["owners"] = []
        self.assertIn("Pushes are off", self.run_push())
        self.assertEqual(self.git("--git-dir", self.remote, "log", "-1", "--format=%s", "main"), "one")

    def test_a_non_fast_forward_push_is_refused(self):
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=self.clone)
        open(os.path.join(self.clone, "b.txt"), "w").write("x\n")
        self.git("add", "b.txt", cwd=self.clone)
        self.git("commit", "-qm", "other", cwd=self.clone)
        self.run_push()  # fast-forward from one: fine
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=self.clone)
        open(os.path.join(self.clone, "c.txt"), "w").write("y\n")
        self.git("add", "c.txt", cwd=self.clone)
        self.git("commit", "-qm", "diverged", cwd=self.clone)
        self.assertIn("failed", self.run_push())
        self.assertEqual(self.git("--git-dir", self.remote, "log", "-1", "--format=%s", "main"), "other")
