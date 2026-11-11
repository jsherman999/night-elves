"""elf: command-line front end."""
from __future__ import annotations
import argparse, sys
from datetime import datetime
from . import backends, config, db, notify, policy
from .dispatcher import Night

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="elf", description="Queue tasks for overnight model work.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="queue a task ('-' reads the prompt from stdin)")
    a.add_argument("prompt", nargs="+")
    a.add_argument("-p", "--priority", type=int, default=5, help="1 = most important (default 5)")
    a.add_argument("-t", "--tier", choices=db.TIERS, default="auto")
    a.add_argument("--source", default="cli")
    l = sub.add_parser("list", help="show recent tasks")
    l.add_argument("-s", "--status")
    s = sub.add_parser("show", help="show one task and its result")
    s.add_argument("id", type=int)
    r = sub.add_parser("retry", help="put a failed task back in the queue")
    r.add_argument("id", type=int)
    n = sub.add_parser("run", help="work the queue now (launchd calls this nightly)")
    n.add_argument("--max-tasks", type=int)
    n.add_argument("--dry-run", action="store_true", help="show routing without calling models")
    n.add_argument("--no-notify", action="store_true")
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
        print(f"queued #{db.add_task(conn, text, args.source, args.priority, args.tier)}")
    elif args.cmd == "list":
        for t in db.list_tasks(conn, args.status):
            print(f"#{t['id']:<4} {t['status']:<8} p{t['priority']} {t['tier']:<8} "
                  f"{t['source']:<4} {' '.join(t['prompt'].split())[:60]}")
    elif args.cmd == "show":
        t = db.get(conn, args.id)
        if not t:
            print("no such task", file=sys.stderr)
            return 1
        for k in t.keys():
            print(f"{k}: {t[k]}")
        if t["result_path"]:
            print("\n" + open(t["result_path"]).read())
    elif args.cmd == "retry":
        db.update(conn, args.id, status="queued", attempts=0, last_error=None)
        print(f"requeued #{args.id}")
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
                print(f"#{t['id']}: {policy.route(t, cfg, st, night.ok) or 'skip tonight'}")
            return 0
        import fcntl
        lock = open(cfg["paths"]["db"] + ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another run is already in progress", file=sys.stderr)
            return 1
        stats = night.run(args.max_tasks)
        text = notify.digest(cfg, conn, stats["run_start"])
        print(text)
        if not args.no_notify:
            notify.send(cfg, text)
    return 0

if __name__ == "__main__":
    sys.exit(main())
