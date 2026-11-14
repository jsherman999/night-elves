"""elf: command-line front end."""
from __future__ import annotations
import argparse, sys
from datetime import datetime
from . import attachments, backends, config, db, notify, policy, when
from .dispatcher import Night

def describe(t) -> str:
    at = f"@{when.label(t['run_after'])} " if t["run_after"] and t["status"] == "queued" else ""
    arch = "(archived) " if t["archived"] else ""
    return (f"#{t['id']:<4} {t['status']:<9} p{t['priority']} {t['tier']:<8} "
            f"{t['source']:<4} {at}{arch}{' '.join(t['prompt'].split())[:60]}")

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="elf", description="Queue tasks for overnight model work.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="queue a task ('-' reads the prompt from stdin)")
    a.add_argument("prompt", nargs="+")
    a.add_argument("-p", "--priority", type=int, default=5, help="1 = most important (default 5)")
    a.add_argument("-t", "--tier", choices=db.TIERS, default="auto")
    a.add_argument("--source", default="cli")
    a.add_argument("--at", help="not before this local time: 01:30, 2pm, or 2026-11-12 01:30")
    a.add_argument("--done-when", "--criteria", dest="criteria", metavar="TEXT",
                   help="what the result must satisfy; the reviewer and the agent are held to it")
    a.add_argument("--from", dest="refs", action="append", default=[], metavar="ID",
                   help="attach the result of an earlier task (repeatable, or 9,12)")
    a.add_argument("-f", "--file", "-i", "--image", dest="files", action="append", default=[],
                   metavar="FILE", help=f"attach a file (repeatable, up to {attachments.MAX_FILES})")
    l = sub.add_parser("list", help="show recent tasks")
    l.add_argument("-s", "--status")
    f = sub.add_parser("search", help="find tasks by words in their prompt or result")
    f.add_argument("words", nargs="+", help="every word must appear; case is ignored")
    for name, text in (("archive", "take finished tasks off the web page's recent list (results are kept)"),
                       ("unarchive", "put archived tasks back on the recent list")):
        x = sub.add_parser(name, help=text)
        x.add_argument("id", type=int, nargs="+")
    s = sub.add_parser("show", help="show one task and its result")
    s.add_argument("id", type=int)
    r = sub.add_parser("retry", help="put a failed, cancelled or finished task back in the queue")
    r.add_argument("id", type=int)
    c = sub.add_parser("cancel", help="take queued tasks out of the queue (retry brings one back)")
    c.add_argument("id", type=int, nargs="+")
    e = sub.add_parser("edit", help="change a task that hasn't started")
    e.add_argument("id", type=int)
    e.add_argument("--prompt", help="new prompt ('-' reads it from stdin)")
    e.add_argument("-p", "--priority", type=int)
    e.add_argument("-t", "--tier", choices=db.TIERS)
    e.add_argument("--done-when", "--criteria", dest="criteria", metavar="TEXT",
                   help="what the result must satisfy ('' clears it)")
    w = e.add_mutually_exclusive_group()
    w.add_argument("--at", help="run at this local time instead")
    w.add_argument("--now", action="store_true", help="run within a minute (the backend's window still applies)")
    w.add_argument("--tonight", action="store_true", help="drop the scheduled time; run in the nightly run")
    n = sub.add_parser("run", help="work the queue now (launchd calls this nightly)")
    n.add_argument("--max-tasks", type=int)
    n.add_argument("--dry-run", action="store_true", help="show routing without calling models")
    n.add_argument("--no-notify", action="store_true")
    n.add_argument("--due-only", action="store_true",
                   help="only scheduled tasks that are due; quiet when there are none")
    sub.add_parser("budget", help="show API-credit pacing")
    sub.add_parser("serve", help="start the web/SMS intake server")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    if args.cmd == "serve":
        from . import web
        web.serve(cfg)
        return 0
    conn = db.connect(cfg["paths"]["db"])
    now = datetime.now().astimezone()

    if args.cmd == "add":
        text = sys.stdin.read() if args.prompt == ["-"] else " ".join(args.prompt)
        try:
            after = db.iso(when.parse_at(args.at, now)) if args.at else None
            kw = dict(source=args.source, priority=args.priority, tier=args.tier, run_after=after,
                      criteria=args.criteria)
            refs = attachments.parse_refs(args.refs)
            with attachments.Staged(cfg) as staged:
                for f in args.files:
                    staged.add_path(f)
                tid, nfiles = attachments.queue(cfg, conn, text, staged, refs, **kw)
        except (OSError, ValueError) as e:
            print(e, file=sys.stderr)
            return 2
        print(f"queued #{tid}" + (f" for {when.label(after)}" if after else "")
              + (f" with {nfiles} file(s)" if nfiles else ""))
    elif args.cmd == "list":
        for t in db.list_tasks(conn, args.status):
            print(describe(t))
    elif args.cmd == "search":
        found = db.search(conn, " ".join(args.words))
        for t, snippet in found:
            print(describe(t) + (f"\n      {snippet}" if snippet else ""))
        if not found:
            print("no task matches", file=sys.stderr)
            return 1
    elif args.cmd in ("archive", "unarchive"):
        try:
            for tid in args.id:
                db.archive(conn, tid, args.cmd == "archive")
                print(f"{args.cmd}d #{tid}")
        except (LookupError, ValueError) as e:
            print(str(e).strip("'\""), file=sys.stderr)
            return 1
    elif args.cmd == "show":
        t = db.get(conn, args.id)
        if not t:
            print("no such task", file=sys.stderr)
            return 1
        for k in t.keys():
            print(f"{k}: {t[k]}")
        if t["result_path"]:
            print("\n" + open(t["result_path"]).read())
    elif args.cmd in ("retry", "cancel", "edit"):
        try:
            if args.cmd == "retry":
                db.requeue(conn, args.id)
                print(f"requeued #{args.id}")
            elif args.cmd == "cancel":
                for tid in args.id:
                    db.cancel(conn, tid)
                    print(f"cancelled #{tid}")
            else:
                fields = {k: v for k, v in (("priority", args.priority), ("tier", args.tier),
                                            ("criteria", args.criteria)) if v is not None}
                if args.prompt is not None:
                    fields["prompt"] = sys.stdin.read() if args.prompt == "-" else args.prompt
                if args.at:
                    fields["run_after"] = db.iso(when.parse_at(args.at, now))
                elif args.now:
                    fields["run_after"] = db.iso(now)
                elif args.tonight:
                    fields["run_after"] = None
                if not fields:
                    print("nothing to change; see elf edit --help", file=sys.stderr)
                    return 2
                db.change(conn, args.id, vision=attachments.local_vision(cfg), **fields)
                print(describe(db.get(conn, args.id)))
        except (LookupError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
    elif args.cmd == "budget":
        st = policy.snapshot(cfg, conn, now, db.iso(now))
        print(f"mode: {st.mode}   cycle {st.elapsed:.0%} elapsed, {st.days_left} day(s) left\n"
              f"API credit: ${st.spent:.2f} spent of ${st.credit:.2f}, ${st.remaining:.2f} left\n"
              f"allowance for a run starting now: ${st.tonight_allowance:.2f}")
    elif args.cmd == "run":
        built = backends.build(cfg)
        night = Night(cfg, conn, built)
        if args.dry_run:
            st = night.state()
            print(f"mode={st.mode} allowance=${st.tonight_allowance:.2f}")
            for t in reversed(db.list_tasks(conn, "queued")):
                wait = (f"waits until {when.label(t['run_after'])}"
                        if t["run_after"] and t["run_after"] > db.iso(now) else None)
                print(f"#{t['id']}: {wait or policy.route(t, cfg, st, night.ok) or 'skip tonight'}")
            return 0
        import fcntl
        lock = open(cfg["paths"]["db"] + ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if args.due_only:  # the nightly run is working; it picks up due tasks itself
                return 0
            # A daytime run (a local agent task, say) may still be going at 01:00: wait for it
            # rather than skip the night.
            print("waiting for the run in progress to finish", file=sys.stderr, flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        stats = night.run(args.max_tasks, due_only=args.due_only)
        if args.due_only and not stats["done"] + stats["failed"] + stats["retried"]:
            return 0
        text = notify.digest(cfg, conn, stats["run_start"])
        print(text)
        if not args.no_notify:
            notify.send(cfg, text, f"digest-{now:%Y-%m-%d-%H%M}" if args.due_only else None)
    return 0

if __name__ == "__main__":
    sys.exit(main())
