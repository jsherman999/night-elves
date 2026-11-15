"""HTML for the phone pages: sign-in, the queue, and one task. Everything shown is escaped here."""
from __future__ import annotations
import json
from datetime import datetime
from html import escape as e
from urllib.parse import quote
from . import attachments, db, md

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e1dc;--accent:#4b5bd6;
--queued:#8a6d00;--queued-bg:#fff4cc;--run:#1f5fbf;--run-bg:#dde9ff;--done:#1d7a3a;--done-bg:#dcf3e3;
--bad:#b3261e;--bad-bg:#fde2df;--off:#6b6b70;--off-bg:#ecebe8;--edge:#bdb9b0;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececef;--mute:#9a9aa2;
--line:#313136;--accent:#8f9bff;--queued:#f0cc5a;--queued-bg:#3a3014;--run:#8fb6ff;--run-bg:#1b2c4a;
--done:#7fd69a;--done-bg:#173322;--bad:#ff8f86;--bad-bg:#45201d;--off:#a6a6ad;--off-bg:#2a2a2e;
--edge:#5a5a64;--on-accent:#11121c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:40em;margin:0 auto;padding:16px 16px 48px}
h1{font-size:1.4em;margin:.2em 0 .6em}h2{font-size:.8em;text-transform:uppercase;letter-spacing:.06em;
color:var(--mute);margin:1.6em 0 .5em}
a{color:var(--accent);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.row{display:block;color:inherit;padding:10px 14px;border-top:1px solid var(--line)}
.row:first-child{border-top:0}.list{padding:0;overflow:hidden}
.row.act{display:flex;gap:10px;align-items:center}.row.act>a{flex:1;min-width:0;color:inherit}
.row.act form{margin:0}button.mini{width:auto;margin:0;padding:4px 10px;font-size:.8em}
.snip{font-size:.85em;color:var(--mute);margin-top:2px;overflow-wrap:anywhere}
.search{display:flex;gap:8px}.search input{flex:1}.search button{width:auto}
.top{display:flex;gap:8px;align-items:center;font-size:.85em;color:var(--mute)}
.top b{color:var(--ink)}.txt{margin-top:2px;overflow-wrap:anywhere}
.pill{font-size:.75em;font-weight:600;padding:1px 8px;border-radius:99px}
.queued{color:var(--queued);background:var(--queued-bg)}.planning,.running,.review{color:var(--run);
background:var(--run-bg)}.done{color:var(--done);background:var(--done-bg)}
.failed{color:var(--bad);background:var(--bad-bg)}.cancelled{color:var(--off);background:var(--off-bg)}
textarea,input,select,button{font:inherit;color:inherit;width:100%;margin:.25em 0;padding:9px 10px;
border:1px solid var(--line);border-radius:9px;background:var(--card)}
button{background:var(--accent);color:var(--on-accent);border-color:transparent;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
button.quiet{background:transparent;color:var(--ink);border-color:var(--edge)}
button.danger{background:transparent;color:var(--bad);border-color:var(--bad)}
button.quiet:hover{background:var(--off-bg);filter:none}button.danger:hover{background:var(--bad-bg);filter:none}
:is(a,button,input,textarea,select,summary):focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.acts{display:grid;gap:4px;margin-top:8px}
.msg{background:var(--run-bg);color:var(--run);border-radius:9px;padding:8px 12px;margin-bottom:12px}
.err{color:var(--bad)}.mute{color:var(--mute);font-size:.9em}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,Menlo,monospace;margin:0}
label{font-size:.85em;color:var(--mute)}
.hint{font-size:.85em;color:var(--mute);margin:.1em 0 .4em}
input[type=file]{padding:7px 10px}
.md{overflow-wrap:anywhere}.md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{font-size:1.05em;margin:1.1em 0 .4em;text-transform:none;
letter-spacing:0;color:var(--ink)}.md h1{font-size:1.25em}.md h2{font-size:1.12em}
.md p,.md ul,.md ol,.md blockquote,.md pre,.md .tbl{margin:.55em 0}.md ul,.md ol{padding-left:1.4em}
.md li{margin:.15em 0}.md li>ul,.md li>ol{margin:.15em 0}
.md code{font:.88em ui-monospace,Menlo,monospace;background:var(--off-bg);padding:.1em .35em;
border-radius:5px}.md pre{background:var(--off-bg);padding:10px 12px;border-radius:9px;overflow-x:auto}
.md pre code{background:none;padding:0;font-size:13px;white-space:pre}
.md blockquote{border-left:3px solid var(--line);padding-left:10px;color:var(--mute)}
.md hr{border:0;border-top:1px solid var(--line);margin:1em 0}
.md .tbl{overflow-x:auto;max-width:100%}.md table{border-collapse:collapse;font-size:.92em;width:max-content}
.md th,.md td{border:1px solid var(--line);padding:4px 8px;vertical-align:top;text-align:left;
max-width:18em;overflow-wrap:break-word}
.md th{background:var(--off-bg)}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin:.6em 0}
.imgs img{width:100%;height:160px;object-fit:cover;object-position:top;border-radius:9px;
border:1px solid var(--line);background:var(--card);display:block}
.use{font-size:.85em;color:var(--mute);margin:.2em 0}.use b{color:var(--ink);font-weight:600}
.scroll{overflow-x:auto;padding:0}
.th{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}.th h1{margin:0;flex:1;min-width:6em}
.task .acts{display:flex;flex-wrap:wrap;gap:8px;flex:1 1 100%;margin:0}.task .acts form{flex:1;margin:0}
.task .acts button,.task .acts p{margin:0}
.facts{display:flex;flex-wrap:wrap;gap:8px 26px;margin:.7em 0 .5em}
.facts dt{font-size:.75em;color:var(--mute)}.facts dd{margin:0;font-size:.92em;overflow-wrap:anywhere}
.alert{background:var(--bad-bg);color:var(--bad);border-radius:9px;padding:8px 12px;margin:.6em 0;
overflow-wrap:anywhere}
.tp{display:grid;grid-template-columns:minmax(0,1fr);gap:0 28px;align-items:start}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}.btns button{margin:0}
.help{font-size:.82em;color:var(--mute);margin:.6em 0 0}
.calls{list-style:none;margin:0;padding:0}.calls li{padding:8px 14px;border-top:1px solid var(--line)}
.calls li:first-child{border-top:0}.calls .l1{display:flex;gap:12px;justify-content:space-between}
.calls .l1 span{font-variant-numeric:tabular-nums;white-space:nowrap}
.calls .l2{font-size:.82em;color:var(--mute)}
details.fold{border:1px solid var(--line);border-radius:12px;background:var(--card);margin:.5em 0}
details.fold>summary{cursor:pointer;padding:9px 14px;font-weight:600;font-size:.92em;border-radius:12px}
details.fold>pre{padding:0 14px 12px}details.fold>pre.log{max-height:26em;overflow:auto;font-size:12px}
.guide{border-left:3px solid var(--accent);padding-left:12px;margin:0 0 .9em}.guide p{margin:.2em 0 .5em}
@media (min-width:900px){main{max-width:min(1400px,94vw);padding:24px 32px 64px}
.home{display:grid;grid-template-columns:minmax(300px,24em) minmax(0,1fr);gap:28px;align-items:start}
.home>form{position:sticky;top:16px}.home h2:first-child{margin-top:.2em}
.imgs{grid-template-columns:repeat(auto-fill,minmax(180px,1fr))}.imgs img{height:220px}
.task button{width:auto;padding:8px 18px}.task .acts{flex:0 1 auto}.task .acts form{flex:none}
.tp{grid-template-columns:minmax(0,1fr) minmax(22em,31em)}
.tp-task{grid-column:1;grid-row:1}.tp-side{grid-column:2;grid-row:1/span 2}.tp-result{grid-column:1;grid-row:2}
.tp>div>h2:first-child{margin-top:1.1em}}
"""

def page(title: str, body: str, msg: str | None = None) -> str:
    flash = f'<div class=msg role=status>{e(msg)}</div>' if msg else ""
    return ('<!doctype html><html lang=en><meta charset=utf-8>'
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            '<meta name=apple-mobile-web-app-title content="Elves">'
            '<link rel=apple-touch-icon href=/apple-touch-icon.png><link rel=icon href=/icon.png>'
            f'<title>{e(title)}</title><style>{CSS}</style><main>{flash}{body}</main></html>')

def local(utc_iso: str) -> datetime:
    return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone()

def when_text(t) -> str:
    if t["status"] == "queued":
        return f"at {local(t['run_after']):%a %H:%M}" if t["run_after"] else "tonight"
    return f"{local(t['updated_at']):%a %H:%M}"

def login(error: str | None = None) -> str:
    err = f'<p class=err>{e(error)}</p>' if error else ""
    return page("Night Elves", f"""<h1>night-elves</h1>{err}
<form method=post action=/login class=card>
<label for=token>Token (ELVES_TOKEN on the Studio)</label>
<input id=token name=token type=password autocomplete=current-password required autofocus>
<button>Sign in</button></form>
<p class=mute>This browser stays signed in for a year. Changing the token signs everyone out.</p>""")

TIER_HELP = {
    "auto": "Elves pick: usually the local model, with Claude planning and checking in the second "
            "half of the billing cycle. Images and PDFs go to the local vision model if there is "
            "one, else Claude. Tasks that fail locally twice go to Claude.",
    "local": "Only the Studio's local models. Free, runs any time. Researches on the web in "
             "checked stages when that is set up. Images and PDFs of up to 20 pages go to the local vision "
             "model, if one is set up.",
    "frontier": "Claude does the whole task, in the overnight window while tonight's allowance lasts.",
    "agent": "An agent with tools in a sandbox: runs commands, writes files, builds small apps, "
             "works through big data files, and must pass its own check. The local agent goes "
             "first when one is set up; Claude Code takes priority 1-3 tasks and ones the local "
             "agent failed twice, in the overnight window, a few per night.",
}
TIER_OPTS = tuple(TIER_HELP)
_TIER_JS = ("var h={" + ",".join(f"{t}:{json.dumps(v)}" for t, v in TIER_HELP.items()) + "};"
            "document.getElementById('tierhelp').textContent=h[this.value]")

def tier_select(current="auto") -> str:
    tip = e("\n".join(f"{t}: {v}" for t, v in TIER_HELP.items()))
    return (f'<select name=tier id=tier title="{tip}" aria-describedby=tierhelp onchange="{e(_TIER_JS)}">'
            + "".join(f"<option{' selected' if t == current else ''} title=\"{e(TIER_HELP[t])}\">{t}"
                      for t in TIER_OPTS) + "</select>")

def tier_help(current="auto") -> str:
    return f"<p class=hint id=tierhelp>{e(TIER_HELP.get(current, ''))}</p>"

def duration(secs) -> str:
    if secs is None:
        return ""
    s = int(round(secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"

def tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 10**6 else f"{n / 1e3:.1f}k" if n >= 10**4 else f"{n:,}"

def cost(u) -> str:
    """What the work cost: API credit billed, plus what subscription or flat-rate runs would
    have cost at API prices. Local models cost nothing."""
    parts = []
    if u["usd"] >= 0.005:
        parts.append(f"${u['usd']:.2f} API credit")
    if u["est"] - u["usd"] >= 0.005:
        parts.append(f"${u['est'] - u['usd']:.2f} unbilled (a subscription run, at API prices)")
    return " + ".join(parts) or "$0 (local)"

def usage_line(t, u, short=False) -> str:
    """Time, tokens and cost for a finished task; "" when nothing was recorded."""
    took = duration(t["secs"] if "secs" in t.keys() else None)
    if short:
        bits = [took] if took else []
        if u and u["in"] + u["out"]:
            bits.append(f"{tokens(u['in'] + u['out'])} tok")
        if u and u["est"] >= 0.005:
            bits.append(f"${u['est']:.2f}" + ("" if u["usd"] >= 0.005 else " est"))
        return " · ".join(bits)
    if not took and not u:
        return ""
    bits = [f"<b>Time</b> {e(took or 'not recorded')}"]
    if u:
        bits.append(f"<b>Tokens</b> {u['in'] + u['out']:,} ({u['in']:,} in, {u['out']:,} out)")
        bits.append(f"<b>Cost</b> {e(cost(u))}")
        by = "; ".join(f"{e(b)} {tokens(v['in'] + v['out'])} tok"
                       + (f" ${v['est']:.2f}" if v["est"] >= 0.005 else "")
                       for b, v in u["by"].items())
        return (f"<p class=use>{' · '.join(bits)}</p><p class=use>By model: {by}</p>")
    return f"<p class=use>{' · '.join(bits)}</p>"

def home(rows, msg: str | None = None, usage=None, q: str = "", found=None, archived=None) -> str:
    """found: [(task, snippet)] for the search q. archived: the archived tasks, when asked for."""
    waiting = sorted((t for t in rows if t["status"] == "queued"),
                     key=lambda t: (t["run_after"] is None, t["run_after"] or "", t["priority"], t["id"]))
    running = [t for t in rows if t["status"] in db.ACTIVE]
    recent = [t for t in rows if t["status"] not in ("queued", *db.ACTIVE)
              and not t["archived"]][:30]
    usage = usage or {}
    def section(name, items, empty, act=None, snippets=None):
        inner = "".join(row(t, usage.get(t["id"]), act, (snippets or {}).get(t["id"]))
                        for t in items) or f'<div class="row mute">{empty}</div>'
        return f"<h2>{name}</h2><div class='card list'>{inner}</div>"
    extra = ""
    if found is not None:
        extra += section(f"Matches for “{e(q)}”", [t for t, _ in found],
                         "No task's prompt or result has those words.",
                         "use", {t["id"]: s for t, s in found})
        extra += ('<p class=hint>Use adds a task\'s number to “Use results of tasks” for the '
                  'task you are writing. <a href=/>Clear search</a></p>')
    if archived is not None:
        extra += section("Archived", archived, "Nothing archived.", "unarchive")
        extra += "<p class=hint><a href=/>Hide archived</a></p>"
    return page("Night Elves", f"""<h1>night-elves</h1><div class=home>
<form method=post action=/tasks class=card enctype=multipart/form-data>
<textarea name=prompt rows=4 placeholder="What should the elves do?" required></textarea>
<div class=two>{tier_select()}<input name=priority type=number min=1 max=9 value=5 aria-label=priority></div>
{tier_help()}
<input name=at placeholder="run at (optional): 01:30, 2pm, 2026-11-20 07:05">
<textarea name=criteria rows=2 placeholder="Done when (optional): e.g. the tests pass and there is a screenshot of each form"></textarea>
<input name=from id=from placeholder="Use results of tasks (optional): 9, 12">
<label for=files>Files or images (optional, up to {attachments.MAX_FILES})</label>
<input id=files name=files type=file multiple>
<p class=hint>Text files up to 200 KB work with any tier. Images and PDFs need Claude or the
local vision model. Bigger or other kinds of files need agent.</p>
<button>Queue it</button></form><div>
<form method=get action=/ class=search role=search>
<input name=q type=search value="{e(q)}" placeholder="Search past prompts and results" aria-label="Search past results">
<button class=quiet>Search</button></form>
{extra}
{section("Running", running, "Nothing running.") if running else ""}
{section("Waiting", waiting, "The queue is empty.")}
{section("Recent", recent, "Nothing yet.", "archive")}
{"" if archived is not None else "<p class=hint><a href=/?archived=1>Show archived tasks</a></p>"}
<p class=hint><a href=/dashboard>Dashboard: model runs, cost, status and logs</a> ·
<a href=/workflow>Explain workflow?</a></p>
<form method=post action=/logout><button class=quiet>Sign out</button></form></div></div>""", msg)

_USE_JS = ("var f=document.getElementById('from'),v=f.value.split(/[^0-9]+/).filter(Boolean);"
           "if(v.indexOf('%d')<0)v.push('%d');f.value=v.join(', ');"
           "f.scrollIntoView({block:'center'});this.textContent='Added'")

def row(t, u=None, act: str | None = None, snippet: str | None = None) -> str:
    """act: "archive", "unarchive" or "use" puts that button on the row, where the task allows
    it. Use adds the task's number to the form's "Use results of tasks" box."""
    pri = f" · p{t['priority']}" if t["priority"] != 5 else ""
    n = len(attachments.files(t))
    pri += f" · {n} file{'s' if n > 1 else ''}" if n else ""
    if t["status"] in ("done", "failed") and (use := usage_line(t, u, short=True)):
        pri += f" · {use}"
    arch = "<span class='pill cancelled'>archived</span>" if t["archived"] else ""
    snip = f"<div class=snip>{e(snippet)}</div>" if snippet else ""
    inner = (f"<div class=top><b>#{t['id']}</b>"
             f"<span class='pill {e(t['status'])}'>{e(t['status'])}</span>{arch}"
             f"<span>{e(t['tier'])}{pri} · {e(when_text(t))}</span></div>"
             f"<div class=txt>{e(' '.join(t['prompt'].split())[:140])}</div>{snip}")
    if act == "use" and t["result_path"]:
        return (f"<div class='row act'><a href=/t/{t['id']}>{inner}</a>"
                f"<button type=button class='quiet mini' aria-label='Use the result of #{t['id']}' "
                f"onclick=\"{e(_USE_JS % (t['id'], t['id']))}\">Use</button></div>")
    if act not in ("archive", "unarchive") or (act == "archive" and t["status"]
                                               not in ("done", "failed", "cancelled")):
        return f"<a class=row href=/t/{t['id']}>{inner}</a>"
    back = "home" if act == "archive" else "archived"
    return (f"<div class='row act'><a href=/t/{t['id']}>{inner}</a>"
            f"<form method=post action=/tasks/{t['id']}><input type=hidden name=action value={act}>"
            f"<input type=hidden name=back value={back}>"
            f"<button class='quiet mini' aria-label='{act.capitalize()} #{t['id']}'>"
            f"{act.capitalize()}</button></form></div>")

def run_record(t, detail: dict) -> str:
    """The record of a task's runs: each model call, the reviewer's or check's feedback, the
    manager's plan and the log lines that name the task. The long ones fold away."""
    out = "<h2>Run record</h2>"
    if calls := detail.get("calls"):
        rows = []
        for c in calls:
            est = c["est_usd"] if c["est_usd"] is not None else c["usd"]
            price = (f"${c['usd']:.2f} billed" if c["usd"] >= 0.005
                     else f"${est:.2f} unbilled" if est and est >= 0.005 else "")
            took = duration(c["secs"])
            rows.append(f"<li><div class=l1><div><b>{e(c['backend'])}</b> {e(c['role'] or '')}</div>"
                        f"<span>{e(price)}</span></div><div class=l2>{local(c['ts']):%a %b %d %H:%M}"
                        + (f", {e(took)}" if took else "")
                        + f", {tokens(c['tokens_in'])} tokens in, {tokens(c['tokens_out'])} out</div></li>")
        out += (f"<div class='card list'><ul class=calls aria-label='Model calls'>{''.join(rows)}</ul></div>"
                "<p class=hint>Model calls, oldest first. Billed was charged to the API credit; "
                "unbilled is what a subscription run would have cost at API prices.</p>")
    else:
        out += "<p class=hint>No model has worked on this task yet.</p>"
    if t["feedback"]:
        out += ("<details class=fold open><summary>Feedback on the last attempt</summary>"
                f"<pre>{e(t['feedback'])}</pre></details>")
    if detail.get("plan"):
        out += f"<details class=fold><summary>The manager's plan</summary><pre>{e(detail['plan'])}</pre></details>"
    log = detail.get("log") or []
    if log:
        out += (f"<details class=fold><summary>Log lines for this task ({len(log)})</summary>"
                f"<pre class=log>{e(chr(10).join(log))}</pre></details>")
    else:
        out += "<p class=hint>Nothing in the logs names this task.</p>"
    return out

def guide_block(g: dict | None) -> str:
    """What a Guide / fix has found so far (see guide.py)."""
    if not g:
        return ""
    who = e(g.get("backend") or "the guide")
    if g["state"] == "pending":
        return (f"<p class=msg role=status>{who} is reading this run. The guide appears here when "
                "it is written; this page checks every few seconds.</p>"
                "<script>setTimeout(function(){location.replace(location.pathname)},6000)</script>")
    if g["state"] == "failed":
        return (f"<p class=alert role=alert>The guide could not be written ({who}): "
                f"{e(g.get('error') or 'no reason given')}</p>")
    return (f"<div class=guide><p><b>Guide from {who}</b></p><p>{e(g.get('diagnosis') or '')}</p>"
            + ("<p class=mute>Its suggested instructions are in the box below. Change them as you "
               "like before sending.</p>" if g.get("instructions")
               else "<p class=mute>It suggested no further instructions.</p>") + "</div>")

def task(t, result: str | None, msg: str | None = None, raw: bool = False, shots=(),
         usage=None, detail: dict | None = None, guide: dict | None = None,
         readonly: bool = False, elsewhere: str = "") -> str:
    """One task: what it is, what to do with it, how its runs went, and its result. On a wide
    screen the task and its result read down the left and the actions and run record sit
    beside them; on a phone it is one column, with the result last.

    detail: {"calls", "log", "plan"} for the run record. guide: the task's Guide / fix, if one
    was asked for. readonly: the page the view-only listener serves, with nothing to press and
    no files; elsewhere is the full site's address, to point to."""
    tid, status = t["id"], t["status"]
    keys = t.keys()

    def button(action, label, cls="", confirm=None):
        js = f" onclick=\"return confirm('{e(confirm)}')\"" if confirm else ""
        return (f"<form method=post action=/tasks/{tid}><input type=hidden name=action value={action}>"
                f"<button class='{cls}'{js}>{e(label)}</button></form>")

    acts = []
    if not readonly:
        if status == "queued":
            acts.append(button("now", "Run now"))
        if status in ("failed", "cancelled", "done"):
            acts.append(button("retry", "Run again" if status == "done" else "Put back in the queue"))
        if t["archived"]:
            acts.append(button("unarchive", "Unarchive", "quiet"))
        elif status in ("done", "failed", "cancelled"):
            acts.append(button("archive", "Archive", "quiet"))
        if status in ("queued", "failed"):
            acts.append(button("cancel", "Cancel task", "danger", f"Cancel #{tid}?"))
        if status in db.ACTIVE:
            acts.append("<p class=mute>A run is working on this task, so it can't be changed.</p>")
    facts = [("Tier", t["tier"]), ("Priority", t["priority"]), ("From", t["source"]),
             ("Added", f"{local(t['created_at']):%a %b %d %H:%M}")]
    if status == "queued":
        facts.append(("Runs", when_text(t)))
    if t["backend"]:
        facts.append(("Ran on", t["backend"]))
    if "needs" in keys and t["needs"]:
        facts.append(("Sized up as", t["needs"]))
    if t["attempts"]:
        facts.append(("Failed attempts", t["attempts"]))
    if "timeouts" in keys and t["timeouts"]:
        facts.append(("Ran out of time", f"{t['timeouts']}x"))
    if t["archived"]:
        facts.append(("Kept", "archived"))
    head = (f"<div class=th><h1>#{tid} <span class='pill {e(status)}'>{e(status)}</span></h1>"
            f"<div class=acts>{''.join(acts)}</div></div><dl class=facts>"
            + "".join(f"<div><dt>{e(k)}</dt><dd>{e(str(v))}</dd></div>" for k, v in facts) + "</dl>")
    if status not in ("queued", *db.ACTIVE):
        head += usage_line(t, usage)
    if t["last_error"]:
        head += f"<p class=alert>Last error: {e(t['last_error'])}</p>"

    # The task itself: its prompt (a form while it can still be edited) and its files.
    fs = attachments.files(t)
    if readonly or status not in db.EDITABLE:
        about = (f"<details class=fold{'' if result else ' open'}><summary>Prompt</summary>"
                 f"<pre>{e(t['prompt'])}</pre></details>")
        if t["criteria"]:
            about += f"<p><b>Done when:</b> {e(t['criteria'])}</p>"
    else:
        at = f"{local(t['run_after']):%Y-%m-%d %H:%M}" if t["run_after"] else ""
        about = f"""<form method=post action=/tasks/{tid} class=card>
<input type=hidden name=action value=save>
<label for=prompt>Prompt</label><textarea id=prompt name=prompt rows=6 required>{e(t['prompt'])}</textarea>
<div class=two><div><label for=tier>Tier</label>{tier_select(t['tier'])}</div>
<div><label for=priority>Priority (1 = first)</label>
<input id=priority name=priority type=number min=1 max=9 value={t['priority']}></div></div>
{tier_help(t['tier'])}
<label for=criteria>Done when (optional)</label>
<textarea id=criteria name=criteria rows=2>{e(t['criteria'] or '')}</textarea>
<label for=at>Run at (empty = the nightly run)</label>
<input id=at name=at value="{e(at)}" placeholder="01:30, 2pm, 2026-11-20 07:05">
<div class=btns><button>Save changes</button></div></form>"""
        if t["criteria"]:
            about += f"<p class=mute><b>Done when:</b> {e(t['criteria'])}</p>"
    if fs and readonly:
        about += f"<p class=mute>{len(fs)} attached file{'s' if len(fs) > 1 else ''} (not shown at this address).</p>"
    elif fs:
        imgs = [i for i, f in enumerate(fs, 1) if f["kind"] == "image"]
        if imgs:
            about += "<div class=imgs>" + "".join(
                f"<a href=/t/{tid}/f/{i}><img src=/t/{tid}/f/{i} alt='{e(fs[i - 1]['name'])}' "
                "loading=lazy></a>" for i in imgs) + "</div>"
        others = [(i, f) for i, f in enumerate(fs, 1) if f["kind"] != "image"]
        if others:
            about += "<div class='card list'>" + "".join(
                f"<a class=row href=/t/{tid}/f/{i}><b>{e(f['name'])}</b> "
                f"<span class=mute>{e(f['kind'])} · {e(attachments.size_label(f.get('size')))}"
                "</span></a>" for i, f in others) + "</div>"

    # Beside it: running it again, and the record of its runs.
    side = ""
    if readonly:
        where = (f"<a href='{e(elsewhere.rstrip('/'))}/t/{tid}'>{e(elsewhere.rstrip('/'))}/t/{tid}</a>"
                 if elsewhere else "the Tailscale address")
        side += (f"<h2>Changing this task</h2><p class=hint>This address only shows. To run the task "
                 f"again with more instructions, ask for a guide, or change it, open {where}.</p>")
    elif status not in db.ACTIVE:
        ran = bool(result or t["last_error"] or t["feedback"])
        pending = bool(guide and guide["state"] == "pending")
        again = "Add these instructions" if status == "queued" else "Resubmit with these instructions"
        ask = ("<button class=quiet name=action value=guide formnovalidate>Guide / fix</button>"
               if ran and not pending else "")
        help_ = ("Guide / fix has a frontier model read this run and write suggested instructions "
                 "into the box. Nothing is sent to the queue until you resubmit."
                 if ran else "A guide can be asked for once the task has run.")
        side += (f"""<h2>Run it again with more instructions</h2><form method=post action=/tasks/{tid} class=card>
{guide_block(guide)}<label for=extra>Added to the end of the prompt. An agent carries on in the same workspace.</label>
<textarea id=extra name=extra rows=7 required placeholder="What should it do differently this time?">{e((guide or {}).get('instructions') or '')}</textarea>
<div class=btns><button name=action value=resubmit>{again}</button>{ask}</div>
<p class=help>{help_}</p></form>""")
    if detail is not None:
        side += run_record(t, detail)

    out = ""
    if result is not None:
        if readonly:
            switch = ""
        else:
            link = f"<a href=/t/{tid}>formatted</a>" if raw else f"<a href=/t/{tid}?raw=1>plain text</a>"
            switch = f"<p class=mute>Show as {link}</p>"
        shown = f"<pre>{e(result)}</pre>" if raw and not readonly else md.render(result)
        out += (f"<h2>Result</h2><div class='card{'' if raw and not readonly else ' md'}'>{shown}</div>"
                + switch)
    if shots and not readonly:
        out += ("<h2>Images in the workspace</h2><div class=imgs>" + "".join(
            f"<a href=/t/{tid}/w/{e(quote(s))}><img src=/t/{tid}/w/{e(quote(s))} alt='{e(s)}' "
            f"title='{e(s)}' loading=lazy></a>" for s in shots) + "</div>")
    back = ("<p><a href=/>&larr; Dashboard</a></p>" if readonly else
            "<p><a href=/>&larr; Queue</a> · <a href=/dashboard>Dashboard</a></p>")
    body = (f"<div class=task>{head}<div class=tp><div class=tp-task><h2>Task</h2>{about}</div>"
            f"<div class=tp-side>{side}</div><div class=tp-result>{out}</div></div>{back}</div>")
    return page(f"Task {tid}", body, msg)
