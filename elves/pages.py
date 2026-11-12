"""HTML for the phone pages: sign-in, the queue, and one task. Everything shown is escaped here."""
from __future__ import annotations
import json
from datetime import datetime
from html import escape as e
from . import attachments, db, md

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e1dc;--accent:#4b5bd6;
--queued:#8a6d00;--queued-bg:#fff4cc;--run:#1f5fbf;--run-bg:#dde9ff;--done:#1d7a3a;--done-bg:#dcf3e3;
--bad:#b3261e;--bad-bg:#fde2df;--off:#6b6b70;--off-bg:#ecebe8}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececef;--mute:#9a9aa2;
--line:#313136;--accent:#8f9bff;--queued:#f0cc5a;--queued-bg:#3a3014;--run:#8fb6ff;--run-bg:#1b2c4a;
--done:#7fd69a;--done-bg:#173322;--bad:#ff8f86;--bad-bg:#45201d;--off:#a6a6ad;--off-bg:#2a2a2e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:40em;margin:0 auto;padding:16px 16px 48px}
h1{font-size:1.4em;margin:.2em 0 .6em}h2{font-size:.8em;text-transform:uppercase;letter-spacing:.06em;
color:var(--mute);margin:1.6em 0 .5em}
a{color:var(--accent);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.row{display:block;color:inherit;padding:10px 14px;border-top:1px solid var(--line)}
.row:first-child{border-top:0}.list{padding:0;overflow:hidden}
.top{display:flex;gap:8px;align-items:center;font-size:.85em;color:var(--mute)}
.top b{color:var(--ink)}.txt{margin-top:2px;overflow-wrap:anywhere}
.pill{font-size:.75em;font-weight:600;padding:1px 8px;border-radius:99px}
.queued{color:var(--queued);background:var(--queued-bg)}.planning,.running,.review{color:var(--run);
background:var(--run-bg)}.done{color:var(--done);background:var(--done-bg)}
.failed{color:var(--bad);background:var(--bad-bg)}.cancelled{color:var(--off);background:var(--off-bg)}
textarea,input,select,button{font:inherit;color:inherit;width:100%;margin:.25em 0;padding:9px 10px;
border:1px solid var(--line);border-radius:9px;background:var(--card)}
button{background:var(--accent);color:#fff;border:0;font-weight:600}
button.quiet{background:var(--off-bg);color:var(--ink)}button.danger{background:var(--bad-bg);color:var(--bad)}
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
            "half of the billing cycle. Tasks that fail locally twice, or have images or PDFs, go "
            "to Claude.",
    "local": "Only the Studio's local model. Free, runs any time, text only (no images or PDFs).",
    "frontier": "Claude does the whole task, in the overnight window while tonight's allowance lasts.",
    "agent": "Claude Code with tools in a sandbox: runs commands, writes files, builds small apps, "
             "works through big data files. Overnight window only, a few per night.",
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

def home(rows, msg: str | None = None) -> str:
    waiting = sorted((t for t in rows if t["status"] == "queued"),
                     key=lambda t: (t["run_after"] is None, t["run_after"] or "", t["priority"], t["id"]))
    running = [t for t in rows if t["status"] in db.ACTIVE]
    recent = [t for t in rows if t["status"] not in ("queued", *db.ACTIVE)][:30]
    def section(name, items, empty):
        inner = "".join(row(t) for t in items) or f'<div class="row mute">{empty}</div>'
        return f"<h2>{name}</h2><div class='card list'>{inner}</div>"
    return page("Night Elves", f"""<h1>night-elves</h1>
<form method=post action=/tasks class=card enctype=multipart/form-data>
<textarea name=prompt rows=4 placeholder="What should the elves do?" required></textarea>
<div class=two>{tier_select()}<input name=priority type=number min=1 max=9 value=5 aria-label=priority></div>
{tier_help()}
<input name=at placeholder="run at (optional): 01:30, 2pm, 2026-11-20 07:05">
<label for=files>Files or images (optional, up to {attachments.MAX_FILES})</label>
<input id=files name=files type=file multiple>
<p class=hint>Text files up to 200 KB work with any tier. Images and PDFs need Claude (not local).
Bigger or other files need agent.</p>
<button>Queue it</button></form>
{section("Running", running, "Nothing running.") if running else ""}
{section("Waiting", waiting, "The queue is empty.")}
{section("Recent", recent, "Nothing yet.")}
<form method=post action=/logout><button class=quiet>Sign out</button></form>""", msg)

def row(t) -> str:
    pri = f" · p{t['priority']}" if t["priority"] != 5 else ""
    n = len(attachments.files(t))
    pri += f" · {n} file{'s' if n > 1 else ''}" if n else ""
    return (f"<a class=row href=/t/{t['id']}><div class=top><b>#{t['id']}</b>"
            f"<span class='pill {e(t['status'])}'>{e(t['status'])}</span>"
            f"<span>{e(t['tier'])}{pri} · {e(when_text(t))}</span></div>"
            f"<div class=txt>{e(' '.join(t['prompt'].split())[:140])}</div></a>")

def task(t, result: str | None, msg: str | None = None, raw: bool = False) -> str:
    tid, status = t["id"], t["status"]
    facts = [f"tier {t['tier']}", f"priority {t['priority']}", f"from {t['source']}",
             f"added {local(t['created_at']):%a %b %d %H:%M}"]
    if t["backend"]:
        facts.append(f"ran on {t['backend']}")
    if t["attempts"]:
        facts.append(f"{t['attempts']} attempt(s)")
    head = (f"<h1>#{tid} <span class='pill {e(status)}'>{e(status)}</span></h1>"
            f"<p class=mute>{e(' · '.join(facts))}</p>")
    if status == "queued":
        head += f"<p><b>Runs {e(when_text(t))}</b></p>"
    if t["last_error"]:
        head += f"<p class=err>Last error: {e(t['last_error'])}</p>"
    if fs := attachments.files(t):
        imgs = [i for i, f in enumerate(fs, 1) if f["kind"] == "image"]
        if imgs:
            head += "<div class=imgs>" + "".join(
                f"<a href=/t/{tid}/f/{i}><img src=/t/{tid}/f/{i} alt='{e(fs[i - 1]['name'])}' "
                "loading=lazy></a>" for i in imgs) + "</div>"
        others = [(i, f) for i, f in enumerate(fs, 1) if f["kind"] != "image"]
        if others:
            head += "<div class='card list'>" + "".join(
                f"<a class=row href=/t/{tid}/f/{i}><b>{e(f['name'])}</b> "
                f"<span class=mute>{e(f['kind'])} · {e(attachments.size_label(f.get('size')))}"
                "</span></a>" for i, f in others) + "</div>"

    def button(action, label, cls="", confirm=None):
        js = f" onclick=\"return confirm('{e(confirm)}')\"" if confirm else ""
        return (f"<form method=post action=/tasks/{tid}><input type=hidden name=action value={action}>"
                f"<button class='{cls}'{js}>{e(label)}</button></form>")

    body = head
    if status in db.EDITABLE:
        at = f"{local(t['run_after']):%Y-%m-%d %H:%M}" if t["run_after"] else ""
        body += f"""<form method=post action=/tasks/{tid} class=card>
<input type=hidden name=action value=save>
<label for=prompt>Prompt</label><textarea id=prompt name=prompt rows=6 required>{e(t['prompt'])}</textarea>
<div class=two><div><label for=tier>Tier</label>{tier_select(t['tier'])}</div>
<div><label for=priority>Priority (1 = first)</label>
<input id=priority name=priority type=number min=1 max=9 value={t['priority']}></div></div>
{tier_help(t['tier'])}
<label for=at>Run at (empty = the nightly run)</label>
<input id=at name=at value="{e(at)}" placeholder="01:30, 2pm, 2026-11-20 07:05">
<button>Save changes</button></form>"""
    acts = []
    if status == "queued":
        acts.append(button("now", "Run now"))
    if status in ("failed", "cancelled", "done"):
        acts.append(button("retry", "Run again" if status == "done" else "Put back in the queue"))
    if status in ("queued", "failed"):
        acts.append(button("cancel", "Cancel task", "danger", f"Cancel #{tid}?"))
    if status in db.ACTIVE:
        acts.append("<p class=mute>A run is working on this task, so it can't be changed.</p>")
    body += f"<div class=acts>{''.join(acts)}</div>"
    if result is not None:
        switch = (f"<a href=/t/{tid}>formatted</a>" if raw else f"<a href=/t/{tid}?raw=1>plain text</a>")
        shown = f"<pre>{e(result)}</pre>" if raw else md.render(result)
        body += (f"<h2>Result</h2><div class='card{'' if raw else ' md'}'>{shown}</div>"
                 f"<p class=mute>Show as {switch}</p>")
    body += "<p><a href=/>&larr; Queue</a></p>"
    return page(f"Task {tid}", body, msg)
