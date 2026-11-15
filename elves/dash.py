"""The dashboard: what is running, what each model did and cost, the queue, and the logs.

One read-only page built from the queue, the spend ledger and the log files beside them. The
web intake serves it at /dashboard to a signed-in browser; `[web] view_port` also serves it,
and nothing else, to the local network behind its own token (see web.serve)."""
from __future__ import annotations
import fcntl, json, re, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape as e
from pathlib import Path
from . import db, pages, policy

DAYS = 14          # of the charts
RUNS = 80          # model calls listed
TASKS = 30
LOG_LINES = 300    # of each log, from its end
LOGS = ("nightly", "due", "web")  # <name>.log beside the queue; launchd writes them
SLOTS = 8          # series colours; the backends past these share the last
ITEMS = 40         # lines in a cost breakdown

ROLE_TEXT = {
    "local": "does ordinary tasks",
    "frontier_api": "Claude for planning, checking and escalated tasks, paid from the API credit",
    "frontier_sub": "Claude on the subscription, used when the API can't be (no allowance left tonight)",
    "second_opinion": "reviews the local model's answers",
    "agent": "the agent for hard agent tasks and ones the local agent failed",
    "local_vision": "reads images and PDFs on this Mac",
    "local_review": "reviews local answers when no other reviewer is free",
    "local_agent": "tries agent tasks first",
    "agent_review": "reads the local agent's work before it is accepted",
    "agent_handoff": "leaves the next run a note when the local agent stops unfinished",
    "agent_plan": "plans the local agent's work",
    "classifier": "sizes up auto tasks as agent, web or answer",
    "guide": "writes the guide when you press Guide / fix on a task",
}

def about(cfg: dict, name: str, roles: list[str]) -> str:
    """What a backend is and how it is paid for, in a sentence or two."""
    c = cfg["backends"].get(name)
    if not c:
        return "No longer in config.toml; its past runs are still in the ledger."
    argv = c.get("argv", [])
    if c.get("type") == "openai_compat":
        what = (f"A model ({c.get('model') or 'unnamed'}) served on this Mac at "
                f"{c.get('base_url', '?')}. It costs nothing and can run at any hour.")
    elif c.get("type") == "anthropic_api":
        what = (f"Claude ({c.get('model') or 'default model'}) through your Anthropic API key. Every "
                f"call is billed per token against the ${cfg['budget']['monthly_credit_usd']:,.0f} "
                "monthly API credit, within a nightly allowance. This is the only backend that "
                "spends money.")
    elif "opencode" in argv:
        what = ("opencode driving a local model with tools, inside a sandbox. It costs nothing; a "
                "run count and a time limit hold it back.")
    elif "codex" in argv:
        what = ("The Codex CLI on your ChatGPT plan. Nothing is billed per call: the plan's own "
                "limits apply, and a run count here.")
    elif "claude" in argv:
        tools = "with tools, in a sandbox, " if name == cfg["roles"].get("agent") else ""
        what = (f"Claude Code run from the command line {tools}on your claude.ai subscription. "
                "Nothing is billed per call: “unbilled” is what its tokens would have cost at API "
                "prices. Its hours and a run count hold it back instead.")
    else:
        what = "A command-line tool that takes a prompt and prints an answer. Not billed per call."
    if not c.get("enabled", True):
        what += " It is switched off in config.toml."
    return what

CSS = """
:root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;
--s8:#e34948;--grid:#e9e7e2}
@media (prefers-color-scheme:dark){:root{--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;
--s5:#d55181;--s6:#0ca30c;--s7:#9085e9;--s8:#e66767;--grid:#2c2c31}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.tile .k{font-size:.75em;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.tile .v{font-size:1.5em;font-weight:650;line-height:1.25;overflow-wrap:anywhere}
.tile .s{font-size:.8em;color:var(--mute)}
.scroll{overflow-x:auto;padding:0}
table.d{border-collapse:collapse;width:100%;font-size:.8em}
table.d th,table.d td{padding:5px 8px;border-top:1px solid var(--line);text-align:left;
vertical-align:top;white-space:nowrap}
table.d .s2{display:block;font-size:.92em;color:var(--mute);white-space:normal}
.tile.click{cursor:pointer}.tile.click:hover,.tile.click:focus-visible{border-color:var(--accent);outline:0}
.tile .more{font-size:.75em;color:var(--accent);margin-top:2px}
dialog{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:0;
width:min(860px,94vw);max-height:86vh}dialog::backdrop{background:rgba(0,0,0,.5)}
dialog>div{padding:16px 18px}dialog h3{margin:0 0 .3em;font-size:1.1em}
dialog h4{margin:1em 0 .3em;font-size:.8em;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
dialog p{font-size:.88em;margin:.3em 0 .6em}dialog ul{font-size:.88em;margin:.3em 0;padding-left:1.3em}
dialog form{margin:.9em 0 0;text-align:right}dialog form button{width:auto;padding:6px 18px}
table.d tr:first-child th{border-top:0;color:var(--mute);font-weight:600;font-size:.9em}
table.d td.n,table.d th.n{text-align:right;font-variant-numeric:tabular-nums}
table.d td.w{white-space:normal;min-width:12em;overflow-wrap:anywhere}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:.82em;color:var(--mute);margin:.2em 0 .6em}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px}
.chart h3{font-size:.95em;margin:0 0 2px}.chart p{font-size:.8em;color:var(--mute);margin:0 0 6px}
.chart svg{display:block;width:100%;height:auto}
.chart text{font:11px -apple-system,system-ui,sans-serif;fill:var(--mute)}
.chart .g{stroke:var(--grid);stroke-width:1}.chart .hit{fill:transparent}
.chart .hit:hover{fill:var(--ink);fill-opacity:.06}
#tip{position:fixed;z-index:9;pointer-events:none;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:9px;padding:6px 10px;font-size:.8em;white-space:pre;
box-shadow:0 4px 14px rgba(0,0,0,.18);display:none}
.ok{color:var(--done)}.no{color:var(--bad)}.idle{color:var(--mute)}
.logs summary{cursor:pointer;font-weight:600;padding:2px 0}
.logs pre{max-height:26em;overflow:auto;background:var(--off-bg);border-radius:9px;padding:10px 12px;
font-size:12px}.logs input{max-width:24em}
.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:.85em;color:var(--mute)}
.bar a{font-weight:600}
"""

JS = """
(function(){var tip=document.getElementById('tip');
document.addEventListener('mousemove',function(ev){var t=ev.target.closest&&ev.target.closest('[data-tip]');
if(!t){tip.style.display='none';return}tip.textContent=t.getAttribute('data-tip');tip.style.display='block';
var x=ev.clientX+14,y=ev.clientY+14,w=tip.offsetWidth,h=tip.offsetHeight;
if(x+w>innerWidth-8)x=ev.clientX-w-14;if(y+h>innerHeight-8)y=ev.clientY-h-14;
tip.style.left=Math.max(4,x)+'px';tip.style.top=Math.max(4,y)+'px'});
document.addEventListener('click',function(ev){var t=ev.target.closest&&ev.target.closest('[data-open]');
if(t){ev.preventDefault();var g=document.getElementById(t.getAttribute('data-open'));
if(g&&g.showModal){tip.style.display='none';g.showModal()}return}
if(ev.target.tagName==='DIALOG')ev.target.close()});
document.addEventListener('keydown',function(ev){var t=ev.target;
if((ev.key==='Enter'||ev.key===' ')&&t.matches&&t.matches('.tile[data-open]')){ev.preventDefault();t.click()}});
var f=document.getElementById('logfilter');
function filt(){var q=f.value.toLowerCase();document.querySelectorAll('.logs pre').forEach(function(p){
if(!p.dataset.all)p.dataset.all=p.textContent;
p.textContent=q?p.dataset.all.split('\\n').filter(function(l){return l.toLowerCase().indexOf(q)>=0}).join('\\n'):p.dataset.all})}
if(f){f.addEventListener('input',filt)}
document.querySelectorAll('.logs pre').forEach(function(p){p.scrollTop=p.scrollHeight});
setInterval(function(){if(document.hidden||(f&&f.value)||scrollY>80||document.querySelector('dialog[open]'))return;location.reload()},60000)})();
"""

def _ping(url: str) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "night-elves"}),
                         timeout=1.5) as r:
            return r.status < 500
    except OSError:
        return False

def services(cfg: dict) -> list[tuple[str, str, bool]]:
    """(name, what it is, whether it answers) for the local servers the backends lean on."""
    todo = []
    for name, c in cfg["backends"].items():
        if not c.get("enabled", True):
            continue
        if c.get("type") == "openai_compat" and c.get("base_url"):
            todo.append(("models", "local model server", c["base_url"].rstrip("/") + "/models"))
        if c.get("search_url"):
            todo.append(("search", "SearXNG", c["search_url"].rstrip("/") + "/"))
        if c.get("reader_url"):
            todo.append(("browser", "Jev reader", c["reader_url"].rstrip("/") + "/health"))
    seen, uniq = set(), []
    for t in todo:
        if t[2] not in seen:
            seen.add(t[2])
            uniq.append(t)
    with ThreadPoolExecutor(max_workers=8) as pool:
        up = list(pool.map(_ping, [u for _, _, u in uniq]))
    return [(n, what, ok) for (n, what, _), ok in zip(uniq, up)]

def run_in_progress(cfg: dict) -> bool:
    """Whether `elf run` holds the queue's lock right now."""
    try:
        with open(cfg["paths"]["db"] + ".lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass
    return False

def tail(path: Path, lines: int = LOG_LINES, block: int = 256 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - block, 0))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    # Terminal progress bars (a model loading) arrive as one huge line of escape codes.
    keep = [l[:400] for l in text.splitlines() if "\x1b[" not in l]
    return "\n".join(keep[-lines:])

def gather(cfg: dict, conn, now: datetime | None = None, ping=True) -> dict:
    now = now or datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today, week = db.iso(midnight), db.iso(midnight - timedelta(days=6))
    state = policy.snapshot(cfg, conn, now, today)
    names = list(cfg["backends"])

    def totals(since: str) -> dict:
        return {r[0]: {"calls": r[1], "in": r[2] or 0, "out": r[3] or 0, "secs": r[4] or 0.0,
                       "usd": r[5] or 0.0, "est": r[6] or 0.0}
                for r in conn.execute(
                    "SELECT backend, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(secs), SUM(usd),"
                    " SUM(COALESCE(est_usd, usd)) FROM spend WHERE ts>=? GROUP BY backend", (since,))}
    by = {"today": totals(today), "week": totals(week), "cycle": totals(state.cycle_start)}
    first = midnight - timedelta(days=DAYS - 1)
    # Backends since removed from the config still have history, in the totals or the charts.
    for t in (*by.values(), totals(db.iso(first))):
        names += [n for n in t if n not in names]

    def itemize(since: str, amount: str) -> list:
        """What added money to a bucket: one line per task, backend and kind of call."""
        return conn.execute(
            f"SELECT s.task_id, s.backend, s.role, COUNT(*) AS calls, SUM({amount}) AS usd, "
            "MAX(s.ts) AS ts, SUM(s.secs) AS secs, t.prompt FROM spend s "
            f"LEFT JOIN tasks t ON t.id=s.task_id WHERE s.ts>=? AND {amount}>0 "
            "GROUP BY s.task_id, s.backend, s.role ORDER BY usd DESC", (since,)).fetchall()
    items = {"api": itemize(state.cycle_start, "s.usd"),
             "sub": itemize(today, "(COALESCE(s.est_usd, s.usd) - s.usd)")}
    roles: dict[str, list[str]] = {}
    for role, name in cfg["roles"].items():
        if role == "classifier":  # "" = the local model sizes tasks up; "off" = nobody does
            name = None if name == "off" else name or cfg["roles"]["local"]
        if name:
            roles.setdefault(name, []).append(role)
    by_role: dict[str, list] = {}
    for r in conn.execute(
            "SELECT backend, COALESCE(role, '') AS role, COUNT(*) AS calls, SUM(secs) AS secs, "
            "SUM(usd) AS usd, SUM(COALESCE(est_usd, usd)) AS est FROM spend WHERE ts>=? "
            "GROUP BY backend, role ORDER BY calls DESC", (db.iso(first),)):
        by_role.setdefault(r["backend"], []).append(r)
    recent = {n: conn.execute(
        "SELECT s.ts, s.task_id, s.role, s.secs, s.usd, s.est_usd, t.prompt FROM spend s "
        "LEFT JOIN tasks t ON t.id=s.task_id WHERE s.backend=? ORDER BY s.rowid DESC LIMIT 8",
        (n,)).fetchall() for n in names}
    days = [(first + timedelta(days=i)).date() for i in range(DAYS)]
    daily = {d: {} for d in days}
    for ts, backend, secs, usd, est in conn.execute(
            "SELECT ts, backend, secs, usd, COALESCE(est_usd, usd) FROM spend WHERE ts>=?",
            (db.iso(first),)):
        cell = daily.get(pages.local(ts).date())
        if cell is not None:
            c = cell.setdefault(backend, {"calls": 0, "secs": 0.0, "usd": 0.0, "est": 0.0})
            c["calls"] += 1
            c["secs"] += secs or 0.0
            c["usd"] += usd or 0.0
            c["est"] += est or 0.0

    backends = []
    for n in names:
        c = cfg["backends"].get(n)
        row = {"name": n, "type": (c or {}).get("type", "removed"), "model": "", "limit": "",
               "state": "removed"}
        if c:
            argv = c.get("argv", [])
            row["model"] = (c.get("model") or (argv[argv.index("-m") + 1] if "-m" in argv[:-1] else "")
                            or next((a for a in argv if a in ("claude", "codex", "opencode")),
                                    argv[0] if argv else ""))
            on = c.get("enabled", True)
            ok = on and policy.available(n, cfg, conn, state, today, now)
            row["state"] = "ready" if ok else "off" if not on else "waiting"
            lim = []
            if c.get("window"):
                lim.append(f"{c['window'][0]:02d}:00-{c['window'][1]:02d}:00")
            if c.get("type") == "command":
                lim.append(f"{db.runs_since(conn, n, today)} of {c.get('max_runs_per_night', 5)} runs today")
            if c.get("type") == "anthropic_api":
                lim.append(f"${state.tonight_left:.2f} left tonight")
            row["limit"] = ", ".join(lim)
        backends.append(row)

    counts = dict(conn.execute("SELECT status, COUNT(*) FROM tasks WHERE archived=0 GROUP BY status"))
    finished = dict(conn.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE status IN ('done','failed') AND updated_at>=? "
        "GROUP BY status", (today,)))
    tasks = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (TASKS,)).fetchall()
    runs = conn.execute(
        "SELECT s.ts, s.backend, s.task_id, s.role, s.tokens_in, s.tokens_out, s.usd, s.est_usd,"
        " s.secs, t.prompt FROM spend s LEFT JOIN tasks t ON t.id=s.task_id "
        "ORDER BY s.rowid DESC LIMIT ?", (RUNS,)).fetchall()
    home = Path(cfg["paths"]["db"]).parent
    return {
        "now": now, "state": state, "names": names, "by": by, "days": days, "daily": daily,
        "backends": backends, "counts": counts, "finished": finished,
        "active": [t for t in tasks if t["status"] in db.ACTIVE] or conn.execute(
            "SELECT * FROM tasks WHERE status IN ('planning','running','review')").fetchall(),
        "tasks": tasks, "usage": db.usage(conn, [t["id"] for t in tasks]), "runs": runs,
        "running": run_in_progress(cfg), "services": services(cfg) if ping else [],
        "logs": {n: tail(home / f"{n}.log") for n in LOGS},
        "items": items,
        "detail": {n: {"about": about(cfg, n, roles.get(n, [])), "roles": roles.get(n, []),
                       "by_role": by_role.get(n, []), "recent": recent.get(n, [])} for n in names},
    }

def task_log(cfg: dict, tid: int, lines: int = 80) -> list[str]:
    """The lines of the launchd logs that name this task, oldest first, each marked with its
    log. A line that follows one of the task's lines and belongs to no task (the rest of a
    long error) is kept with it."""
    home, mine, out = Path(cfg["paths"]["db"]).parent, re.compile(rf"#{tid}(?!\d)"), []
    other = re.compile(r"\s*#\d|night-elves |API credit|intake #|\S+ \"(GET|POST) ")
    for name in LOGS:
        on = False
        for line in tail(home / f"{name}.log", 100000, 2 * 2**20).splitlines():
            if mine.search(line):
                on = True
            elif on and (other.match(line) or not line.strip()):
                on = False
            if on:
                out.append(f"{name}: {line}")
    return out[-lines:]

def task_calls(conn, tid: int) -> list:
    """The ledger's lines for one task, oldest first."""
    return conn.execute("SELECT ts, backend, role, tokens_in, tokens_out, usd, est_usd, secs "
                        "FROM spend WHERE task_id=? ORDER BY rowid", (tid,)).fetchall()

def _slot(names: list[str], name: str) -> int:
    return min(names.index(name), SLOTS - 1) + 1 if name in names else SLOTS

def _nice(top: float) -> float:
    """A round number at or above top, for the axis."""
    if top <= 0:
        return 1.0
    mag = 10 ** len(str(int(top))) / 10 if top >= 1 else 0.1 if top >= 0.1 else 0.01
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= top:
            return m * mag
    return 10 * mag

def chart(d: dict, key: str, title: str, sub: str, fmt, div: float = 1) -> str:
    """Stacked columns, one a day, a segment per backend. Each day is one hover target that
    lists its numbers; the tables below carry the same figures. div: what to divide the
    ledger's figure by for the axis (60 turns seconds into minutes)."""
    W, H, L, R, T, B = 480, 200, 48, 6, 10, 24
    days, names = d["days"], d["names"]
    sums = [sum(c[key] for c in d["daily"][day].values()) / div for day in days]
    top = _nice(max(sums))
    band = (W - L - R) / len(days)
    bw = min(24, band - 8)
    ph = H - T - B
    out = [f'<svg viewBox="0 0 {W} {H}" role=img aria-label="{e(title)}: {e(sub)}">']
    for i in range(3):
        v = top * i / 2
        y = T + ph - ph * i / 2
        out.append(f'<line class=g x1={L} x2={W - R} y1={y:.1f} y2={y:.1f} />'
                   f'<text x={L - 6} y={y + 4:.1f} text-anchor=end>{e(fmt(v))}</text>')
    for i, day in enumerate(days):
        x = L + band * i + (band - bw) / 2
        cell, y = d["daily"][day], T + ph
        parts = [(n, cell[n][key] / div) for n in names if n in cell and cell[n][key] > 0]
        for j, (n, v) in enumerate(parts):
            h = max(ph * v / top, 1.5)
            y -= h
            gap = 2 if j else 0  # a surface gap between segments, none at the baseline
            hh = max(h - gap, 0.8)
            r = min(4, bw / 2, hh) if j == len(parts) - 1 else 0  # only the data end is rounded
            out.append(f'<path class="s{_slot(names, n)}" fill="var(--s{_slot(names, n)})" d="M{x:.1f},{y + hh:.1f}'
                       f'V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + bw - r:.1f}'
                       f'Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f}V{y + hh:.1f}Z" />')
        if i % 2 == (len(days) - 1) % 2:
            out.append(f'<text x={x + bw / 2:.1f} y={H - 7} text-anchor=middle>{day:%b} {day.day}</text>')
        lines = [f"{day:%a %b} {day.day}"] + [f"{n}: {fmt(v)}" for n, v in reversed(parts)]
        lines.append(f"total: {fmt(sums[i])}" if len(parts) > 1 else "" if parts else "nothing ran")
        out.append(f'<rect class=hit x={L + band * i:.1f} y={T} width={band:.1f} height={ph} '
                   f'data-tip="{e(chr(10).join(l for l in lines if l))}" />')
    out.append("</svg>")
    return f"<div class='card chart'><h3>{e(title)}</h3><p>{e(sub)}</p>{''.join(out)}</div>"

def _money(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.005 or v == 0 else "<$0.01"

def _task_cost(u: dict | None) -> str:
    if not u:
        return "-"
    sub = u["est"] - u["usd"]
    parts = ([_money(u["usd"])] if u["usd"] >= 0.005 else []) + ([f"≈{_money(sub)}"] if sub >= 0.005 else [])
    return " + ".join(parts) or "-"

def _mins(secs: float) -> str:
    return pages.duration(secs) or "0s"

def _pair(t: dict | None) -> str:
    """A cost cell: what was billed, and under it what was not."""
    if not t:
        return "<td class=n>-</td>"
    sub = t["est"] - t["usd"]
    return (f"<td class=n>{e(_money(t['usd']))}"
            + (f"<span class=s2>+{e(_money(sub))} unbilled</span>" if sub >= 0.005 else "") + "</td>")

def _dialog(ident: str, title: str, inner: str) -> str:
    return (f"<dialog id={ident} aria-label='{e(title)}'><div><h3>{e(title)}</h3>{inner}"
            "<form method=dialog><button class=quiet autofocus>Close</button></form></div></dialog>")

def _items(rows, task_ref, empty: str, note: str) -> str:
    """A cost breakdown: the biggest lines first, the small ones summed at the end."""
    if not rows:
        return f"<p>{e(empty)}</p>"
    total = sum(r["usd"] for r in rows)
    out = []
    for r in rows[:ITEMS]:
        n = f" ×{r['calls']}" if r["calls"] > 1 else ""
        out.append(f"<tr><td class=n>{e(_money(r['usd']))}</td><td>{task_ref(r['task_id'])}</td>"
                   f"<td>{e(r['backend'])} {e(r['role'] or '')}{n}</td>"
                   f"<td>{pages.local(r['ts']):%a %b} {pages.local(r['ts']).day} {pages.local(r['ts']):%H:%M}</td>"
                   f"<td class=w>{e(' '.join((r['prompt'] or 'no task').split())[:80])}</td></tr>")
    if len(rows) > ITEMS:
        rest = rows[ITEMS:]
        out.append(f"<tr><td class=n>{e(_money(sum(r['usd'] for r in rest)))}</td>"
                   f"<td colspan=4>{len(rest)} smaller items</td></tr>")
    return (f"<p>{e(note)}</p><div class=scroll><table class=d><tr><th class=n>Cost<th>Task<th>What ran"
            f"<th>Last call<th>Task prompt</tr>{''.join(out)}"
            f"<tr><td class=n><b>{e(_money(total))}</b></td><td colspan=4><b>total</b></td></tr></table></div>")

def render(d: dict, links: bool = True, msg: str | None = None, tasks: bool = True) -> str:
    """links: this is the full site, with the queue to go back to (the view-only server has
    none). tasks: whether task numbers link to task pages."""
    st, now, names = d["state"], d["now"], d["names"]
    def task_ref(tid) -> str:
        if tid is None:
            return "-"
        return f"<a href=/t/{tid}>#{tid}</a>" if tasks else f"#{tid}"

    active = d["active"]
    if active:
        t = active[0]
        since = pages.duration((now - pages.local(t["updated_at"])).total_seconds())
        doing = (f"<div class=v>#{t['id']} {e(t['status'])}</div><div class=s>{e(t['backend'] or t['tier'])}"
                 f" · in this step {e(since)}<br>{e(' '.join(t['prompt'].split())[:70])}</div>")
    else:
        doing = ("<div class=v>Working</div><div class=s>a run holds the queue, between tasks</div>"
                 if d["running"] else "<div class=v>Idle</div><div class=s>no run in progress</div>")
    c, fin = d["counts"], d["finished"]
    today = d["by"]["today"]
    calls = sum(t["calls"] for t in today.values())
    secs = sum(t["secs"] for t in today.values())
    sub_today = sum(t["est"] - t["usd"] for t in today.values())
    more = "<div class=more>What added to this ›</div>"
    tiles = [
        ("Now", doing, ""),
        ("Queue", f"<div class=v>{c.get('queued', 0)} waiting</div><div class=s>today: "
                  f"{fin.get('done', 0)} done, {fin.get('failed', 0)} failed</div>", ""),
        ("Model calls today", f"<div class=v>{calls:,}</div><div class=s>{e(_mins(secs))} of model time</div>", ""),
        ("API credit", f"<div class=v>{e(_money(st.spent))}</div><div class=s>of ${st.credit:,.0f} this "
                       f"cycle · {st.days_left} days left<br>today {e(_money(st.tonight_spent))} of "
                       f"{e(_money(st.tonight_allowance))} · {e(st.mode)}</div>{more}", "dlg-api"),
        ("Subscription work today", f"<div class=v>{e(_money(sub_today))}</div>"
                                    f"<div class=s>at API prices; not billed</div>{more}", "dlg-sub"),
    ]
    def tile(k, v, dlg):
        click = f" click' data-open={dlg} role=button tabindex=0 aria-haspopup=dialog" if dlg else "'"
        return f"<div class='tile{click}><div class=k>{e(k)}</div>{v}</div>"
    body = ["<div class=bar><h1 style='margin:0;flex:1'>night-elves dashboard</h1>",
            f"<span>as of {now:%a %b} {now.day}, {now:%H:%M:%S}</span>",
            "<a href=/>Queue</a>" if links else "", "</div>",
            "<div class=tiles style='margin-top:12px'>" + "".join(tile(*t) for t in tiles) + "</div>"]
    dialogs = [
        _dialog("dlg-api", f"API credit used this cycle: {_money(st.spent)}", _items(
            d["items"]["api"], task_ref, "Nothing has been billed to the API credit this cycle.",
            f"Money actually charged to the Anthropic API key since {pages.local(st.cycle_start):%b} "
            f"{pages.local(st.cycle_start).day}. Only the API backend spends it; each line is one "
            "task's calls of one kind.")),
        _dialog("dlg-sub", f"Subscription work today: {_money(sub_today)}", _items(
            d["items"]["sub"], task_ref, "No subscription runs today.",
            "Not charged. This is what today's runs on flat-rate plans (Claude Code on the "
            "claude.ai subscription) would have cost at API prices, from their token counts.")),
    ]

    if d["services"]:
        body.append("<p class=bar style='margin:.8em 0 0'>" + "".join(
            f"<span><b class={'ok' if ok else 'no'}>{'✓ up' if ok else '✗ down'}</b> "
            f"{e(n)} <span class=idle>({e(what)})</span></span>" for n, what, ok in d["services"]) + "</p>")

    legend = "".join(f"<span><i class=sw style='background:var(--s{_slot(names, n)})'></i>{e(n)}</span>"
                     for n in names if any(n in d["daily"][day] for day in d["days"]))
    body.append(f"<h2>Model runs, last {DAYS} days</h2><div class=legend>{legend or 'Nothing has run yet.'}</div>"
                "<div class=charts>"
                + chart(d, "secs", "Model time per day", "minutes of work, by backend",
                        lambda v: f"{v:,.0f}m" if v >= 10 or v == int(v) else f"{v:.1f}m", 60)
                + chart(d, "est", "Cost per day", "US dollars at API prices, by backend "
                        "(subscription and local runs are not billed)", _money)
                + chart(d, "calls", "Model calls per day", "calls, by backend", lambda v: f"{v:,.0f}")
                + "</div>")

    rows = []
    for i, b in enumerate(d["backends"]):
        n = b["name"]
        cls = {"ready": "ok", "waiting": "idle", "off": "idle", "removed": "idle"}[b["state"]]
        mark = {"ready": "✓ ready", "waiting": "… waiting", "off": "– off", "removed": "– removed"}[b["state"]]
        t = d["by"]["today"].get(n)
        rows.append(
            f"<tr><td><i class=sw style='background:var(--s{_slot(names, n)})'></i>"
            f"<a href=#dlg-b{i} data-open=dlg-b{i} aria-haspopup=dialog><b>{e(n)}</b></a>"
            f"<span class='s2 {cls}'>{mark}</span></td>"
            f"<td class=w>{e(b['model'] or b['type'])}<span class=s2>{e(b['limit'])}</span></td>"
            + (f"<td class=n>{t['calls']:,}</td><td class=n>{pages.tokens(t['in'])} / {pages.tokens(t['out'])}</td>"
               f"<td class=n>{e(_mins(t['secs']))}</td>" if t else "<td class=n>0</td><td class=n>-</td><td class=n>-</td>")
            + "".join(_pair(d["by"][k].get(n)) for k in ("today", "week", "cycle")) + "</tr>")
        det = d["detail"][n]
        inner = f"<p>{e(det['about'])}</p>"
        if det["roles"]:
            inner += "<h4>What it is used for</h4><ul>" + "".join(
                f"<li><b>{e(r)}</b>: {e(ROLE_TEXT.get(r, 'a role in config.toml'))}</li>"
                for r in det["roles"]) + "</ul>"
        if b["limit"]:
            inner += f"<h4>Limits</h4><p>{e(b['limit'])}</p>"
        inner += ("<h4>Totals</h4><div class=scroll><table class=d><tr><th>Period<th class=n>Calls"
                  "<th class=n>Tokens in / out<th class=n>Time<th class=n>Cost</tr>")
        for k, label in (("today", "Today"), ("week", "7 days"), ("cycle", "This billing cycle")):
            v = d["by"][k].get(n)
            inner += (f"<tr><td>{label}</td>" + (
                f"<td class=n>{v['calls']:,}</td><td class=n>{pages.tokens(v['in'])} / {pages.tokens(v['out'])}</td>"
                f"<td class=n>{e(_mins(v['secs']))}</td>{_pair(v)}" if v else
                "<td class=n>0</td><td class=n>-</td><td class=n>-</td><td class=n>-</td>") + "</tr>")
        inner += "</table></div>"
        if det["by_role"]:
            inner += (f"<h4>What it did, last {DAYS} days</h4><div class=scroll><table class=d><tr><th>Kind of call"
                      "<th class=n>Calls<th class=n>Time<th class=n>Cost</tr>" + "".join(
                          f"<tr><td>{e(r['role'] or '-')}</td><td class=n>{r['calls']:,}</td>"
                          f"<td class=n>{e(_mins(r['secs'] or 0))}</td>"
                          + _pair({"usd": r["usd"] or 0.0, "est": r["est"] or 0.0}) + "</tr>"
                          for r in det["by_role"]) + "</table></div>")
        if det["recent"]:
            inner += ("<h4>Latest calls</h4><div class=scroll><table class=d><tr><th>Ended<th>Task<th>Kind"
                      "<th class=n>Time<th>Task prompt</tr>" + "".join(
                          f"<tr><td>{pages.local(r['ts']):%a %H:%M}</td><td>{task_ref(r['task_id'])}</td>"
                          f"<td>{e(r['role'] or '')}</td><td class=n>{e(pages.duration(r['secs']) or '-')}</td>"
                          f"<td class=w>{e(' '.join((r['prompt'] or '').split())[:70])}</td></tr>"
                          for r in det["recent"]) + "</table></div>")
        else:
            inner += "<p>It has not run yet.</p>"
        dialogs.append(_dialog(f"dlg-b{i}", n, inner))
    body.append("<h2>Backends</h2><div class='card scroll'><table class=d><tr><th>Backend<th>Model and limits"
                "<th class=n>Calls today<th class=n>Tokens in / out<th class=n>Time"
                "<th class=n>Cost today<th class=n>7 days<th class=n>Cycle</tr>" + "".join(rows) + "</table></div>"
                "<p class=hint>A cost is money billed to the API credit. “+$x unbilled” under it is what "
                "subscription runs would have cost at API prices; nobody was charged that. Waiting means "
                "outside its hours, out of runs, or out of tonight's allowance. Click a backend's name "
                "for what it is and what it has done.</p>")

    rows = []
    for t in d["tasks"]:
        u = d["usage"].get(t["id"])
        tries = (f"{t['attempts']}" if t["attempts"] else "") + (f" +{t['timeouts']} timed out"
                                                                 if t["timeouts"] else "")
        err = f"<div class=err>{e((t['last_error'] or '')[:160])}</div>" if t["last_error"] else ""
        rows.append(f"<tr><td>{task_ref(t['id'])}</td><td><span class='pill {e(t['status'])}'>{e(t['status'])}"
                    f"</span></td><td>{e(t['tier'])}</td><td>{e(t['backend'] or '-')}</td>"
                    f"<td>{pages.local(t['updated_at']):%a %H:%M}</td><td class=n>{e(tries or '-')}</td>"
                    f"<td class=n>{e(pages.duration(t['secs']) or '-')}</td>"
                    f"<td class=n>{e(_task_cost(u))}</td>"
                    f"<td class=w>{e(' '.join(t['prompt'].split())[:110])}{err}</td></tr>")
    body.append("<h2>Tasks, most recently changed</h2><div class='card scroll'><table class=d><tr><th>Task"
                "<th>Status<th>Tier<th>Backend<th>Changed<th class=n>Failed tries<th class=n>Time"
                "<th class=n>Cost<th>Prompt and last error</tr>" + "".join(rows) + "</table></div>"
                "<p class=hint>Cost: a plain figure was billed to the API credit; ≈ marks what "
                "subscription runs would have cost at API prices, which was not billed."
                + (" Click a task's number for its details." if tasks else "") + "</p>")

    rows = []
    for r in d["runs"]:
        est = r["est_usd"] if r["est_usd"] is not None else r["usd"]
        price = (_money(r["usd"]) if r["usd"] >= 0.005 else f"≈{_money(est)}" if est and est >= 0.005 else "-")
        rows.append(f"<tr><td>{pages.local(r['ts']):%a %H:%M:%S}</td><td>{task_ref(r['task_id'])}</td>"
                    f"<td><i class=sw style='background:var(--s{_slot(names, r['backend'])})'></i>{e(r['backend'])}</td>"
                    f"<td>{e(r['role'] or '')}</td><td class=n>{r['tokens_in']:,}</td>"
                    f"<td class=n>{r['tokens_out']:,}</td><td class=n>{e(pages.duration(r['secs']) or '-')}</td>"
                    f"<td class=n>{e(price)}</td><td class=w>{e(' '.join((r['prompt'] or '').split())[:70])}</td></tr>")
    body.append(f"<h2>Model calls, latest {RUNS}</h2><div class='card scroll'><table class=d><tr><th>Ended"
                "<th>Task<th>Backend<th>Role<th class=n>Tokens in<th class=n>Tokens out<th class=n>Time"
                "<th class=n>Cost<th>Task prompt</tr>" + "".join(rows) + "</table></div>"
                "<p class=hint>≈ marks a cost at API prices that was not billed.</p>")

    logs = "".join(f"<details{' open' if n == 'nightly' else ''}><summary>{n}.log</summary>"
                   f"<pre>{e(text) or '(empty)'}</pre></details>" for n, text in d["logs"].items())
    body.append(f"<h2>Logs, last {LOG_LINES} lines each</h2><div class='card logs'>"
                "<input id=logfilter type=search placeholder='Show only lines containing… (pauses refresh)' "
                f"aria-label='Filter log lines'>{logs}</div>"
                "<p class=hint>The page reloads every minute while it is in front, scrolled to the top "
                "and the log filter is empty.</p>")
    if not links:
        body.append("<form method=post action=/logout><button class=quiet style='max-width:12em'>"
                    "Sign out</button></form>")
    body.append("".join(dialogs))
    body.append(f"<div id=tip role=tooltip></div><style>{CSS}</style><script>{JS}</script>")
    return pages.page("Elves dashboard", "".join(body), msg)

def login(error: str | None = None) -> str:
    err = f'<p class=err>{e(error)}</p>' if error else ""
    return pages.page("Elves dashboard", f"""<h1>night-elves dashboard</h1>{err}
<form method=post action=/login class=card>
<label for=token>View token (ELVES_VIEW_TOKEN on the Studio)</label>
<input id=token name=token type=password autocomplete=current-password required autofocus>
<button>Sign in</button></form>
<p class=mute>This address shows the dashboard only, read-only. Queueing and changing tasks is at
the Tailscale address.</p>""")

def to_json(d: dict) -> str:
    """The figures behind the page, for scripts."""
    st = d["state"]
    return json.dumps({
        "as_of": d["now"].isoformat(timespec="seconds"), "running": d["running"],
        "active": [{"id": t["id"], "status": t["status"], "backend": t["backend"]} for t in d["active"]],
        "queue": d["counts"], "finished_today": d["finished"],
        "budget": {"mode": st.mode, "cycle_spent": st.spent, "credit": st.credit,
                   "days_left": st.days_left, "today_spent": st.tonight_spent,
                   "today_allowance": st.tonight_allowance},
        "backends": d["backends"], "totals": d["by"],
        "daily": {str(day): cell for day, cell in d["daily"].items()},
        "services": [{"name": n, "what": w, "up": ok} for n, w, ok in d["services"]],
    })
