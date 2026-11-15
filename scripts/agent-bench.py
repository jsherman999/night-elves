#!/usr/bin/env python3
"""Compare local models as the agent tier's model.

Each model gets the same small coding tasks through the local_agent backend exactly as
night-elves runs it (same command, sandbox, instructions and finish contract), each in a
fresh workspace. A run is scored by tests the agent never sees, run in the check sandbox
after the agent has exited. Models must already be loaded in the model server.

  agent-bench.py qwen3.8-27b:131072 ornith-1.5-35b-a3b-mlx:131072   # model[:context]
  agent-bench.py --reference        # score the built-in solutions: checks the tests themselves
  agent-bench.py openai/gpt-oss-120b --manager codex --plan     # what a manager adds

With --manager, each task goes through the dispatcher itself (check, retries, time limits),
once for the model alone and once with that backend as its manager (roles.agent_review and
agent_handoff; --plan adds a third arm with roles.agent_plan too). Nothing is escalated to
Claude, so the arms differ only in the manager. The manager's calls use its real plan or quota.

Run it with the Python that has night-elves installed. Results go to --out as runs.jsonl.
"""
from __future__ import annotations
import argparse, copy, json, re, shutil, sys, time, uuid
from pathlib import Path
from elves import backends, config, db, verify
from elves.backends import BackendError, Command, TimedOut
from elves.dispatcher import AGENT_SYS, CONTRACT, Night

TASKS = {}

# --- 1. Fix bugs against documented behaviour -------------------------------------------------
TASKS["bugfix"] = {
    "prompt": ("ledger.py in this folder has bugs: several of its functions don't do what their "
               "docstrings say. Find and fix them so every function behaves exactly as "
               "documented. Keep the function names and signatures. Python 3.11+, standard "
               "library only. Write tests for it too."),
    "files": {"ledger.py": '''"""Money helpers for a shared-expenses ledger. Amounts are integer cents."""


def parse_amount(text):
    """Turn a typed amount into cents.

    '12.50' -> 1250, '19.99' -> 1999, '$1,234.5' -> 123450, '7' -> 700, '-3.05' -> -305,
    ' 4.2 ' -> 420. A leading '$' (after any minus sign) and thousands commas are allowed.
    Raises ValueError for anything else: empty text, letters, more than two decimals.
    """
    text = text.strip().replace("$", "").replace(",", "")
    return int(float(text) * 100)


def split_evenly(total_cents, people):
    """Split an amount into `people` shares that add up to it exactly.

    Leftover cents go to the first shares, one each: split_evenly(1000, 3) -> [334, 333, 333].
    A negative total is split as its absolute value, then negated:
    split_evenly(-1000, 3) -> [-334, -333, -333]. people < 1 raises ValueError.
    """
    share = total_cents // people
    shares = [share] * people
    for i in range(total_cents - share * people):
        shares[-1 - i] += 1
    return shares


def settle(balances):
    """Work out who pays whom.

    balances maps a name to cents: positive means the person is owed money, negative means
    they owe. They must add up to 0, otherwise ValueError. Returns a list of
    (debtor, creditor, cents) transfers that brings everyone to 0. At each step the person
    who owes the most pays the person who is owed the most (ties go to the name that comes
    first alphabetically), paying the smaller of the two amounts; repeat until all are 0.
    People at 0 never appear.
    """
    owes = {n: -b for n, b in balances.items() if b < 0}
    owed = {n: b for n, b in balances.items() if b > 0}
    out = []
    while owes and owed:
        d = max(owes, key=lambda n: owes[n])
        c = max(owed, key=lambda n: owed[n])
        pay = min(owes[d], owed[c])
        out.append((d, c, pay))
        owes[d] -= pay
        owed[c] -= pay
        if not owes[d]:
            del owes[d]
        if not owed[c]:
            del owed[c]
    return out
'''},
    "solution": {"ledger.py": '''import re


def parse_amount(text):
    m = re.fullmatch(r"\\s*(-?)\\$?(\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.(\\d{1,2}))?\\s*", text)
    if not m:
        raise ValueError(text)
    sign, whole, frac = m.groups()
    cents = int(whole.replace(",", "")) * 100 + int(((frac or "") + "00")[:2])
    return -cents if sign else cents


def split_evenly(total_cents, people):
    if people < 1:
        raise ValueError(people)
    sign = -1 if total_cents < 0 else 1
    total = abs(total_cents)
    share, extra = divmod(total, people)
    return [sign * (share + (1 if i < extra else 0)) for i in range(people)]


def settle(balances):
    if sum(balances.values()) != 0:
        raise ValueError("balances do not add up to 0")
    owes = {n: -b for n, b in balances.items() if b < 0}
    owed = {n: b for n, b in balances.items() if b > 0}
    out = []
    while owes and owed:
        d = min(owes, key=lambda n: (-owes[n], n))
        c = min(owed, key=lambda n: (-owed[n], n))
        pay = min(owes[d], owed[c])
        out.append((d, c, pay))
        owes[d] -= pay
        owed[c] -= pay
        if not owes[d]:
            del owes[d]
        if not owed[c]:
            del owed[c]
    return out
'''},
    "tests": '''import unittest


class T(unittest.TestCase):
    def setUp(self):
        import ledger
        self.m = ledger

    def test_parse_plain(self):
        self.assertEqual(self.m.parse_amount("12.50"), 1250)
        self.assertEqual(self.m.parse_amount("7"), 700)
        self.assertEqual(self.m.parse_amount(" 4.2 "), 420)

    def test_parse_is_exact(self):
        for text, cents in (("19.99", 1999), ("0.29", 29), ("1.15", 115), ("-3.05", -305),
                            ("4.35", 435), ("8.70", 870)):
            self.assertEqual(self.m.parse_amount(text), cents, text)

    def test_parse_dollar_and_commas(self):
        self.assertEqual(self.m.parse_amount("$1,234.5"), 123450)
        self.assertEqual(self.m.parse_amount("-$20"), -2000)

    def test_parse_rejects(self):
        for bad in ("", "abc", "1.234", "1.2.3", "12 dollars"):
            with self.assertRaises(ValueError, msg=bad):
                self.m.parse_amount(bad)

    def test_split_remainder_goes_first(self):
        self.assertEqual(self.m.split_evenly(1000, 3), [334, 333, 333])
        self.assertEqual(self.m.split_evenly(1001, 4), [251, 250, 250, 250])
        self.assertEqual(self.m.split_evenly(900, 3), [300, 300, 300])

    def test_split_negative(self):
        self.assertEqual(self.m.split_evenly(-1000, 3), [-334, -333, -333])
        self.assertEqual(sum(self.m.split_evenly(-7, 3)), -7)

    def test_split_bad_people(self):
        for n in (0, -2):
            with self.assertRaises(ValueError):
                self.m.split_evenly(100, n)

    def test_settle_simple(self):
        self.assertEqual(self.m.settle({"ann": 500, "bob": -500, "cy": 0}),
                         [("bob", "ann", 500)])

    def test_settle_ties_alphabetical(self):
        got = self.m.settle({"zed": -300, "amy": -300, "kim": 300, "bea": 300})
        self.assertEqual([tuple(t) for t in got], [("amy", "bea", 300), ("zed", "kim", 300)])

    def test_settle_greedy(self):
        got = self.m.settle({"a": -700, "b": -100, "c": 500, "d": 300})
        self.assertEqual([tuple(t) for t in got],
                         [("a", "c", 500), ("a", "d", 200), ("b", "d", 100)])

    def test_settle_must_balance(self):
        with self.assertRaises(ValueError):
            self.m.settle({"a": 100, "b": -50})


if __name__ == "__main__":
    unittest.main()
''',
}

# --- 2. Add a feature and a command line to existing code -------------------------------------
TASKS["feature"] = {
    "prompt": ("stock.py in this folder is a small inventory library. Add a command line to it, "
               "in a new file inv.py beside it, used as `python3 inv.py --file FILE COMMAND ...`. "
               "FILE is the JSON file stock.py loads and saves; it doesn't have to exist yet. "
               "Commands:\n"
               "- `add NAME QTY [--price P]`: add QTY of NAME. --price sets the unit price "
               "(a new item without --price costs 0; an existing item keeps its price unless "
               "--price is given).\n"
               "- `remove NAME QTY`: take QTY of NAME out. An item that reaches 0 is deleted. "
               "If NAME is unknown or there are fewer than QTY, change nothing, print a message "
               "to stderr and exit with status 2.\n"
               "- `list [--low N] [--json]`: one line per item, sorted by name, as "
               "NAME<TAB>QTY<TAB>PRICE with the price to two decimals. --low N shows only items "
               "with quantity at or below N. --json prints instead a JSON array of objects with "
               "keys name, qty, price (same order and filter).\n"
               "- `total`: print the value of the whole stock (sum of qty x price) to two "
               "decimals.\n"
               "QTY must be a whole number above 0, otherwise exit with status 2. Successful "
               "commands exit 0. You'll need to add to stock.py (it has no way to remove or to "
               "filter yet); keep what it already does working. Python 3.11+, standard library "
               "only. Write tests too."),
    "files": {"stock.py": '''"""A tiny inventory kept in a JSON file."""
import json
import os


class Stock:
    def __init__(self, path):
        self.path = path
        self.items = {}  # name -> {"qty": int, "price": float}
        if os.path.exists(path):
            with open(path) as f:
                self.items = json.load(f)

    def add(self, name, qty, price=None):
        item = self.items.setdefault(name, {"qty": 0, "price": 0.0})
        item["qty"] += qty
        if price is not None:
            item["price"] = price

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.items, f, indent=2, sort_keys=True)
'''},
    "solution": {"stock.py": '''"""A tiny inventory kept in a JSON file."""
import json
import os


class Stock:
    def __init__(self, path):
        self.path = path
        self.items = {}
        if os.path.exists(path):
            with open(path) as f:
                self.items = json.load(f)

    def add(self, name, qty, price=None):
        item = self.items.setdefault(name, {"qty": 0, "price": 0.0})
        item["qty"] += qty
        if price is not None:
            item["price"] = price

    def remove(self, name, qty):
        if name not in self.items or self.items[name]["qty"] < qty:
            raise KeyError(name)
        self.items[name]["qty"] -= qty
        if not self.items[name]["qty"]:
            del self.items[name]

    def listing(self, low=None):
        return [(n, i["qty"], i["price"]) for n, i in sorted(self.items.items())
                if low is None or i["qty"] <= low]

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.items, f, indent=2, sort_keys=True)
''', "inv.py": '''import argparse
import json
import sys
from stock import Stock


def qty(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("QTY must be above 0")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("qty", type=qty)
    a.add_argument("--price", type=float)
    r = sub.add_parser("remove")
    r.add_argument("name")
    r.add_argument("qty", type=qty)
    ls = sub.add_parser("list")
    ls.add_argument("--low", type=int)
    ls.add_argument("--json", action="store_true")
    sub.add_parser("total")
    args = ap.parse_args()
    s = Stock(args.file)
    if args.cmd == "add":
        s.add(args.name, args.qty, args.price)
        s.save()
    elif args.cmd == "remove":
        try:
            s.remove(args.name, args.qty)
        except KeyError:
            print(f"not enough {args.name}", file=sys.stderr)
            sys.exit(2)
        s.save()
    elif args.cmd == "list":
        rows = s.listing(args.low)
        if args.json:
            print(json.dumps([{"name": n, "qty": q, "price": p} for n, q, p in rows]))
        else:
            for n, q, p in rows:
                print(f"{n}\\t{q}\\t{p:.2f}")
    else:
        print(f"{sum(i['qty'] * i['price'] for i in s.items.values()):.2f}")


main()
'''},
    "tests": '''import json, os, subprocess, sys, tempfile, unittest


class T(unittest.TestCase):
    def setUp(self):
        self.f = os.path.join(tempfile.mkdtemp(), "stock.json")

    def inv(self, *args):
        return subprocess.run([sys.executable, "inv.py", "--file", self.f, *args],
                              capture_output=True, text=True, timeout=60)

    def ok(self, *args):
        p = self.inv(*args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def seed(self):
        self.ok("add", "widget", "5", "--price", "2.5")
        self.ok("add", "bolt", "100", "--price", "0.1")
        self.ok("add", "anvil", "1", "--price", "80")

    def test_add_and_list_sorted(self):
        self.seed()
        self.assertEqual(self.ok("list").splitlines(),
                         ["anvil\\t1\\t80.00", "bolt\\t100\\t0.10", "widget\\t5\\t2.50"])

    def test_add_keeps_price_unless_given(self):
        self.seed()
        self.ok("add", "widget", "2")
        self.ok("add", "bolt", "1", "--price", "0.25")
        self.ok("add", "nut", "3")
        self.assertEqual(self.ok("list").splitlines(),
                         ["anvil\\t1\\t80.00", "bolt\\t101\\t0.25", "nut\\t3\\t0.00", "widget\\t7\\t2.50"])

    def test_remove(self):
        self.seed()
        self.ok("remove", "widget", "2")
        self.assertIn("widget\\t3\\t2.50", self.ok("list").splitlines())

    def test_remove_to_zero_deletes(self):
        self.seed()
        self.ok("remove", "anvil", "1")
        self.assertEqual(self.ok("list").splitlines(), ["bolt\\t100\\t0.10", "widget\\t5\\t2.50"])

    def test_remove_too_many_changes_nothing(self):
        self.seed()
        before = self.ok("list")
        p = self.inv("remove", "widget", "6")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.strip())
        self.assertEqual(self.ok("list"), before)

    def test_remove_unknown(self):
        self.seed()
        p = self.inv("remove", "gizmo", "1")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.strip())

    def test_low_filter(self):
        self.seed()
        self.assertEqual(self.ok("list", "--low", "5").splitlines(),
                         ["anvil\\t1\\t80.00", "widget\\t5\\t2.50"])

    def test_json(self):
        self.seed()
        self.assertEqual(json.loads(self.ok("list", "--json", "--low", "1")),
                         [{"name": "anvil", "qty": 1, "price": 80}])
        self.assertEqual([i["name"] for i in json.loads(self.ok("list", "--json"))],
                         ["anvil", "bolt", "widget"])

    def test_total(self):
        self.seed()
        self.assertEqual(self.ok("total").strip(), "102.50")

    def test_bad_qty(self):
        for q in ("0", "-1", "two"):
            self.assertEqual(self.inv("add", "x", q).returncode, 2, q)
        self.assertEqual(self.ok("list").strip(), "")

    def test_library_still_works(self):
        import stock
        s = stock.Stock(self.f)
        s.add("pin", 4, 0.5)
        s.save()
        self.assertEqual(stock.Stock(self.f).items, {"pin": {"qty": 4, "price": 0.5}})
        self.assertEqual(self.ok("list").splitlines(), ["pin\\t4\\t0.50"])


if __name__ == "__main__":
    unittest.main()
''',
}

# --- 3. Build a small tool from a written spec ------------------------------------------------
LOG = '''Nov 14 03:12:01 gate sshd[311]: Failed password for invalid user admin from 203.0.113.9 port 40022 ssh2
Nov 14 03:12:04 gate sshd[311]: Failed password for invalid user admin from 203.0.113.9 port 40022 ssh2
Nov 14 03:12:09 gate sshd[315]: Failed password for root from 203.0.113.9 port 40031 ssh2
Nov 14 03:13:00 gate sshd[320]: Accepted publickey for jay from 10.0.0.2 port 51000 ssh2: ED25519 SHA256:abc
Nov 14 03:13:02 gate sshd[320]: pam_unix(sshd:session): session opened for user jay(uid=501) by (uid=0)
Nov 14 03:14:10 gate sshd[330]: Failed password for root from 198.51.100.7 port 2222 ssh2
Nov 14 03:14:15 gate sshd[331]: Failed password for invalid user oracle from 198.51.100.7 port 2223 ssh2
Nov 14 03:15:00 gate sudo:      jay : TTY=ttys001 ; PWD=/Users/jay ; USER=root ; COMMAND=/bin/ls
Nov 14 03:15:30 gate sshd[340]: Failed password for jay from 192.0.2.44 port 6000 ssh2
Nov 14 03:16:00 gate sshd[341]: Accepted password for deploy from 10.0.0.3 port 51010 ssh2
Nov 14 03:16:20 gate sshd[342]: Accepted publickey for jay from 10.0.0.2 port 51020 ssh2: ED25519 SHA256:abc
Nov 14 03:17:00 gate sshd[350]: Connection closed by authenticating user root 203.0.113.9 port 40100 [preauth]
Nov 14 03:17:45 gate sshd[351]: Failed password for invalid user test from 192.0.2.50 port 6001 ssh2
Nov 14 03:18:00 gate sshd[352]: Failed password for root from 2001:db8::7 port 6002 ssh2
'''
TASKS["scratch"] = {
    "prompt": ("Write logsum.py, a command-line tool that summarises SSH logins from an sshd "
               "auth log. sample.log in this folder shows the format. Usage: `python3 logsum.py "
               "FILE [--top N]`. It prints one JSON object:\n"
               '{"failed": {"total": <count>, "by_ip": [[ip, count], ...], "by_user": [[user, '
               'count], ...]}, "accepted": {"total": <count>, "users": [...]}, '
               '"invalid_users": [...], "first": "...", "last": "..."}\n'
               "- Only sshd lines that say `Failed password for [invalid user ]USER from IP "
               "port ...` or `Accepted <method> for USER from IP port ...` count. Every other "
               "line is ignored (sudo lines, session lines, closed connections).\n"
               "- by_ip and by_user are sorted by count, highest first, ties by the ip or user "
               "as text, ascending; each holds at most N entries (--top, default 5). "
               "failed.total counts all failed lines, whatever N is.\n"
               "- accepted.users and invalid_users are sorted, without duplicates. "
               "invalid_users are the users from `invalid user` lines.\n"
               "- first and last are the timestamps (the first 15 characters, like `Nov 14 "
               "03:12:01`) of the first and last counted lines in the file; both null when no "
               "line counts.\n"
               "- IPs may be IPv6.\n"
               "A FILE that can't be read: a message on stderr and exit status 1. Python 3.11+, "
               "standard library only. Write tests too."),
    "files": {"sample.log": LOG},
    "solution": {"logsum.py": '''import argparse
import collections
import json
import re
import sys

LINE = re.compile(r"^(.{15}) \\S+ sshd\\[\\d+\\]: (?:(Failed) password for (invalid user )?(\\S+)"
                  r"|(Accepted) \\S+ for (\\S+)) from (\\S+) port ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args()
    try:
        lines = open(a.file).read().splitlines()
    except OSError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    ips, users = collections.Counter(), collections.Counter()
    ok, invalid, stamps, n_ok = set(), set(), [], 0
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        stamps.append(m.group(1))
        if m.group(2):
            ips[m.group(7)] += 1
            users[m.group(4)] += 1
            if m.group(3):
                invalid.add(m.group(4))
        else:
            n_ok += 1
            ok.add(m.group(6))
    top = lambda c: [list(x) for x in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:a.top]]
    print(json.dumps({"failed": {"total": sum(ips.values()), "by_ip": top(ips), "by_user": top(users)},
                      "accepted": {"total": n_ok, "users": sorted(ok)},
                      "invalid_users": sorted(invalid),
                      "first": stamps[0] if stamps else None,
                      "last": stamps[-1] if stamps else None}))


main()
'''},
    "tests": '''import json, os, subprocess, sys, tempfile, unittest

LOG = %r
EXTRA = """Dec  1 00:00:05 gate sshd[9]: Accepted keyboard-interactive/pam for ops from 10.1.1.1 port 1 ssh2
Dec  1 00:00:09 gate sshd[10]: Failed password for invalid user a from 10.9.9.9 port 2 ssh2
Dec  1 00:00:10 gate sshd[11]: Failed password for invalid user b from 10.9.9.8 port 2 ssh2
Dec  1 00:00:11 gate sshd[12]: Failed password for invalid user c from 10.9.9.7 port 2 ssh2
Dec  1 00:00:12 gate cron[13]: Failed password for nobody from 10.9.9.6 port 2 ssh2
Dec  1 00:00:13 gate sshd[14]: error: maximum authentication attempts exceeded for root from 10.9.9.5 port 2 ssh2 [preauth]
"""


class T(unittest.TestCase):
    def run_tool(self, text, *args):
        f = os.path.join(tempfile.mkdtemp(), "auth.log")
        with open(f, "w") as fh:
            fh.write(text)
        p = subprocess.run([sys.executable, "logsum.py", f, *args], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_failed_total(self):
        self.assertEqual(self.run_tool(LOG)["failed"]["total"], 8)

    def test_by_ip(self):
        self.assertEqual([list(x) for x in self.run_tool(LOG)["failed"]["by_ip"]],
                         [["203.0.113.9", 3], ["198.51.100.7", 2], ["192.0.2.44", 1],
                          ["192.0.2.50", 1], ["2001:db8::7", 1]])

    def test_by_user(self):
        self.assertEqual([list(x) for x in self.run_tool(LOG)["failed"]["by_user"]],
                         [["root", 3], ["admin", 2], ["jay", 1], ["oracle", 1], ["test", 1]])

    def test_top(self):
        d = self.run_tool(LOG, "--top", "2")
        self.assertEqual([list(x) for x in d["failed"]["by_ip"]],
                         [["203.0.113.9", 3], ["198.51.100.7", 2]])
        self.assertEqual(len(d["failed"]["by_user"]), 2)
        self.assertEqual(d["failed"]["total"], 8)

    def test_accepted(self):
        self.assertEqual(self.run_tool(LOG)["accepted"], {"total": 3, "users": ["deploy", "jay"]})

    def test_invalid_users(self):
        self.assertEqual(self.run_tool(LOG)["invalid_users"], ["admin", "oracle", "test"])

    def test_first_last(self):
        d = self.run_tool(LOG)
        self.assertEqual((d["first"], d["last"]), ("Nov 14 03:12:01", "Nov 14 03:18:00"))

    def test_other_lines_and_methods(self):
        d = self.run_tool(EXTRA)
        self.assertEqual(d["accepted"], {"total": 1, "users": ["ops"]})
        self.assertEqual(d["failed"]["total"], 3)
        self.assertEqual([list(x) for x in d["failed"]["by_ip"]],
                         [["10.9.9.7", 1], ["10.9.9.8", 1], ["10.9.9.9", 1]])
        self.assertEqual((d["first"], d["last"]), ("Dec  1 00:00:05", "Dec  1 00:00:11"))

    def test_empty(self):
        d = self.run_tool("Nov 14 03:15:00 gate sudo: nothing here\\n")
        self.assertEqual(d["failed"], {"total": 0, "by_ip": [], "by_user": []})
        self.assertEqual((d["accepted"]["total"], d["first"], d["last"]), (0, None, None))

    def test_missing_file(self):
        p = subprocess.run([sys.executable, "logsum.py", "/nonexistent/auth.log"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.strip())


if __name__ == "__main__":
    unittest.main()
''' % LOG,
}

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
BAD = re.compile(r"\b(failures|errors)=(\d+)")


def score(cfg, c, work: Path, tests: str) -> tuple[int, int, str]:
    """Run the hidden tests in the check sandbox. Returns (passed, total, end of the output)."""
    hidden = work / "_hidden_tests.py"
    hidden.write_text(tests)
    env = backends.environ(c, str(work))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code, out = verify.run_check(f"{sys.executable} _hidden_tests.py", work, env, 300,
                                 verify.secrets(cfg))
    hidden.unlink(missing_ok=True)
    m = RAN.search(out)
    total = int(m.group(1)) if m else 0
    bad = sum(int(n) for _, n in BAD.findall(out[m.end():])) if m else 0
    return max(total - bad, 0), total, out[-600:]


def pipeline(cfg, name, c, manager, roles, task, root: Path, attempts: int) -> dict:
    """One task through the dispatcher, in a queue and workspace of its own under root.
    roles: the manager roles that are on in this arm. Returns the row's fields and the
    workspace."""
    cfg = copy.deepcopy(cfg)
    cfg["paths"] = {"db": str(root / "queue.db"), "results": str(root / "results"),
                    "work": str(root / "work"), "attachments": str(root / "attachments")}
    cfg["backends"] = {name: {**c, "max_runs_per_night": 999, "window": None}}
    if roles:
        cfg["backends"][manager] = {**cfg_backend(manager), "max_runs_per_night": 999,
                                    "window": None}
    cfg["roles"].update({"local_agent": name, "agent": "", "classifier": "off", "agent_plan": "",
                         "agent_review": "", "agent_handoff": "", **{r: manager for r in roles}})
    cfg["limits"]["max_attempts"] = attempts
    cfg["push"] = {"owners": []}
    conn = db.connect(cfg["paths"]["db"])
    tid = db.add_task(conn, task["prompt"], tier="agent")
    work = root / "work" / str(tid)
    work.mkdir(parents=True)
    for rel, text in task["files"].items():
        (work / rel).write_text(text)
    log = open(root / "night.log", "a")
    Night(cfg, conn, backends.build(cfg), log=lambda m: print(m, file=log, flush=True)).run()
    t = db.get(conn, tid)
    calls = {}
    for backend, role, n, tin, tout in conn.execute(
            "SELECT backend, role, COUNT(*), SUM(tokens_in), SUM(tokens_out) FROM spend "
            "GROUP BY backend, role"):
        calls[f"{'manager' if backend == manager and roles else 'agent'}.{role}"] = [n, tin, tout]
    log.close()
    lost = (root / "night.log").read_text().count("going on without it")
    return {"secs": t["secs"] or 0.0, "ended": t["status"], "attempts": t["attempts"],
            "timeouts": t["timeouts"], "calls": calls, "manager_lost": lost,
            "tokens_out": sum(v[2] or 0 for k, v in calls.items() if k.startswith("agent.")),
            "tokens_in": sum(v[1] or 0 for k, v in calls.items() if k.startswith("agent.")),
            "work": work}


def cfg_backend(name):
    c = config.load()["backends"].get(name)
    if not c or not c.get("enabled", True):
        sys.exit(f"--manager {name}: no such backend in config.toml, or it is not enabled")
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="*", help="model id as the model server names it, "
                    "optionally :context (default 65536)")
    ap.add_argument("--reference", action="store_true", help="score the built-in solutions")
    ap.add_argument("--tasks", nargs="*", default=list(TASKS), choices=list(TASKS))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1500, help="seconds per run")
    ap.add_argument("--backend", default=None, help="config backend to copy (roles.local_agent)")
    ap.add_argument("--manager", default=None, help="a backend to try as the model's manager: "
                    "runs each task through the dispatcher with and without it")
    ap.add_argument("--plan", action="store_true", help="with --manager: a third arm where "
                    "the manager also plans")
    ap.add_argument("--attempts", type=int, default=3, help="with --manager: limits.max_attempts")
    ap.add_argument("--out", default="~/.night-elves/bench")
    a = ap.parse_args()
    if not a.models and not a.reference:
        ap.error("name at least one model, or use --reference")
    cfg = config.load()
    name = a.backend or cfg["roles"].get("local_agent") or "local_agent"
    base = cfg["backends"][name]
    out = Path(a.out).expanduser() / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    models = [(m.partition(":")[0], int(m.partition(":")[2] or 65536)) for m in a.models]

    # opencode needs every model listed, with the context it was loaded with.
    oc = json.loads(Path(base["env"]["OPENCODE_CONFIG"]).expanduser().read_text())
    prov = oc["providers"]["lmstudio"]
    prov["models"] = {m: {"name": m, "limit": {"context": ctx, "output": 16384}}
                      for m, ctx in models}
    (out / "opencode.json").write_text(json.dumps(oc, indent=2))
    contract = CONTRACT.format(minutes=max(cfg["limits"]["check_timeout"] // 60, 1))
    system = AGENT_SYS + (f" {base['notes']}" if base.get("notes") else "") + "\n\n" + contract

    rows = []
    arms = [("reference", 0)] if a.reference else models
    managed = bool(a.manager) and not a.reference
    if managed:
        cfg_backend(a.manager)
        kinds = [("", ()), (f"+{a.manager}", ("agent_review", "agent_handoff"))]
        if a.plan:
            kinds.append((f"+{a.manager}+plan", ("agent_plan", "agent_review", "agent_handoff")))
        arms = [(m + k, 0) for m, _ in models for k, _ in kinds]
    for rep in range(1, (1 if a.reference else a.reps) + 1):
        for tname in a.tasks:
            task = TASKS[tname]
            for model, _ in (models if managed else ()):
                for kind, roles in kinds:
                    root = out / re.sub(r"[^A-Za-z0-9._+-]", "_", model + kind) / f"{tname}-{rep}"
                    root.mkdir(parents=True)
                    argv = list(base["argv"])
                    argv[argv.index("-m") + 1] = f"lmstudio/{model}"
                    c = {**base, "argv": argv, "timeout": a.timeout,
                         "env": {**base.get("env", {}), "OPENCODE_CONFIG": str(out / "opencode.json")}}
                    row = {"model": model + kind, "task": tname, "rep": rep,
                           **pipeline(cfg, name, c, a.manager, roles, task, root, a.attempts)}
                    work = row.pop("work")
                    row["said"] = (f"tries={row['attempts'] + (row['ended'] == 'done')} "
                                   f"timeouts={row['timeouts']}")
                    row["passed"], row["total"], row["tests_tail"] = score(cfg, c, work, task["tests"])
                    rows.append(row)
                    with open(out / "runs.jsonl", "a") as f:
                        f.write(json.dumps(row) + "\n")
                    mgr = sum(v[0] for k, v in row["calls"].items() if k.startswith("manager."))
                    print(f"{model + kind:36} {tname:8} #{rep} {row['passed']:>2}/{row['total']:<2} "
                          f"{row['secs']:>7.0f}s {row['ended']:7} {row['said']} manager calls={mgr}"
                          f"{' lost=' + str(row['manager_lost']) if row['manager_lost'] else ''}",
                          flush=True)
            for model, _ in (() if managed else arms):
                work = out / re.sub(r"[^A-Za-z0-9._-]", "_", model) / f"{tname}-{rep}"
                work.mkdir(parents=True)
                for rel, text in task["files"].items():
                    (work / rel).write_text(text)
                row = {"model": model, "task": tname, "rep": rep, "secs": 0.0, "tokens_in": 0,
                       "tokens_out": 0, "ended": "ok", "said": ""}
                c = dict(base)
                if a.reference:
                    for rel, text in task["solution"].items():
                        (work / rel).write_text(text)
                else:
                    argv = list(base["argv"])
                    argv[argv.index("-m") + 1] = f"lmstudio/{model}"
                    c.update(argv=argv, timeout=a.timeout,
                             env={**base.get("env", {}), "OPENCODE_CONFIG": str(out / "opencode.json")})
                    sid = uuid.uuid4().hex
                    args = [x.replace("{session_hex}", sid).replace("{session}", sid)
                            for x in base.get("session_args", [])]
                    t0 = time.monotonic()
                    try:
                        res = Command(name, c).run(f"Task:\n{task['prompt']}", system,
                                                   cwd=str(work), args=args)
                    except TimedOut as e:
                        res, row["ended"] = e.result, "timeout"
                    except BackendError as e:
                        res = getattr(e, "result", None)
                        row["ended"] = f"error: {str(e)[-200:]}"
                    row["secs"] = round(time.monotonic() - t0, 1)
                    if res:
                        row.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out)
                        status, _ = verify.contract(res.text)
                        row["said"] = status or "no STATUS"
                        (work / "_reply.md").write_text(res.text)
                row["passed"], row["total"], row["tests_tail"] = score(cfg, c, work, task["tests"])
                rows.append(row)
                with open(out / "runs.jsonl", "a") as f:
                    f.write(json.dumps(row) + "\n")
                print(f"{model:28} {tname:8} #{rep} {row['passed']:>2}/{row['total']:<2} "
                      f"{row['secs']:>7.0f}s out={row['tokens_out']:<6} {row['ended'][:40]} "
                      f"{row['said']}", flush=True)

    if managed:
        # accepted = the dispatcher called it done; wrong = done, but the hidden tests disagree
        print(f"\n{'arm':36} {'solved':>7} {'tests':>9} {'accepted':>8} {'wrong':>6} {'mean s':>7} "
              f"{'manager calls':>13}")
        for arm, _ in arms:
            mine = [r for r in rows if r["model"] == arm]
            full = lambda r: r["total"] > 0 and r["passed"] == r["total"]
            done = [r for r in mine if r["ended"] == "done"]
            mgr = sum(v[0] for r in mine for k, v in r["calls"].items() if k.startswith("manager."))
            print(f"{arm:36} {sum(map(full, mine)):>3}/{len(mine):<3} "
                  f"{sum(r['passed'] for r in mine):>4}/{sum(r['total'] for r in mine):<4} "
                  f"{len(done):>8} {sum(not full(r) for r in done):>6} "
                  f"{sum(r['secs'] for r in mine) / len(mine):>7.0f} {mgr:>13}")
        print(f"\nruns: {out}/runs.jsonl")
        return
    print(f"\n{'model':28} {'solved':>7} {'tests':>9} {'mean s':>7} {'tok out':>8} {'out tok/s':>9}")
    for model, _ in arms:
        mine = [r for r in rows if r["model"] == model]
        solved = sum(r["total"] > 0 and r["passed"] == r["total"] for r in mine)
        secs = sum(r["secs"] for r in mine)
        tout = sum(r["tokens_out"] for r in mine)
        print(f"{model:28} {solved:>3}/{len(mine):<3} "
              f"{sum(r['passed'] for r in mine):>4}/{sum(r['total'] for r in mine):<4} "
              f"{secs / len(mine):>7.0f} {tout:>8} {tout / secs if secs else 0:>9.1f}")
    print(f"\nruns: {out}/runs.jsonl")
    if a.reference and any(r["passed"] != r["total"] or not r["total"] for r in rows):
        shutil.copy(out / "runs.jsonl", out / "FAILED-reference.jsonl")
        sys.exit(1)


if __name__ == "__main__":
    main()
