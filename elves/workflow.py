"""Explain workflow: flowcharts of what happens to a task, drawn from the live config.

Three figures: how a task reaches a path, how a written answer is produced and checked, and
how an agent task is worked, checked and retried. The boxes name the backends and models this
config.toml actually routes to, so the page stays true when the roles change. The figures are
inline SVG, coloured by who pays for the step.

`python -m elves.workflow DIR` writes the figures as SVG files (for the README), drawn for the
example setup."""
from __future__ import annotations
import re, sys
from html import escape as e
from pathlib import Path
from . import pages

# Who pays for a step decides its colour; the pairs are the page's own status colours.
KINDS = {"step": ("var(--card)", "var(--edge)"), "local": ("var(--done-bg)", "var(--done)"),
         "plan": ("var(--run-bg)", "var(--run)"), "api": ("var(--queued-bg)", "var(--queued)"),
         "fail": ("var(--bad-bg)", "var(--bad)"), "end": ("var(--card)", "var(--ink)")}
LEGEND = (("local", "runs on this Mac: free"),
          ("plan", "a flat-rate plan (claude.ai or ChatGPT): not billed per call"),
          ("api", "the Claude API: billed to the monthly credit"),
          ("fail", "a failure, and what happens next"))
LIGHT = {"--card": "#ffffff", "--edge": "#8d897f", "--ink": "#1d1d1f", "--done-bg": "#dcf3e3",
         "--done": "#1d7a3a", "--run-bg": "#dde9ff", "--run": "#1f5fbf", "--queued-bg": "#fff4cc",
         "--queued": "#8a6d00", "--bad-bg": "#fde2df", "--bad": "#b3261e"}
CHAR = 6.7  # px a character takes at the label size, near enough to check that text fits

CSS = """
.wf figure{margin:0 0 1.2em}.wf figcaption{font-size:.9em;color:var(--mute);margin:.6em 0 0;max-width:62em}
.wf .fig{overflow-x:auto;padding:14px}.wf svg{display:block;width:100%;min-width:760px;max-width:980px;
height:auto;margin:0 auto;color:var(--ink)}
.wf svg text{font:12.5px -apple-system,system-ui,sans-serif;fill:currentColor}
.wf svg .b{font-weight:650}.wf svg .s{font-size:11.5px;opacity:.82}.wf svg .l{font-size:11.5px;font-style:italic}
.wf .legend{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:.85em;color:var(--mute);margin:.2em 0 1em}
.wf .sw{display:inline-block;width:14px;height:14px;border-radius:4px;border:1.5px solid;margin-right:6px;
vertical-align:-3px}
.wf table{border-collapse:collapse;width:100%;font-size:.86em}
.wf th,.wf td{padding:6px 10px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.wf tr:first-child th{border-top:0;color:var(--mute);font-weight:600}
.wf p.lead{max-width:62em}
"""

class Fig:
    """A flowchart on a fixed canvas. Nodes are placed by their centre; lines are drawn
    through the points given, with an arrowhead at the end."""
    def __init__(self, ident: str, w: int, h: int, label: str):
        self.ident, self.w, self.h, self.label = ident, w, h, label
        self.out: list[str] = []
        self.fits: list[tuple[str, float, float]] = []  # (line, its width, the room it has)

    def _text(self, x, y, lines, room):
        n = len(lines)
        top = y - (n - 1) * 7.5 + 4
        for i, line in enumerate(lines):
            cls = "b" if i == 0 else "s"
            self.fits.append((line, len(line) * CHAR, room))
            self.out.append(f'<text class="{cls}" x="{x}" y="{top + i * 15:.1f}" text-anchor="middle">'
                            f"{e(line)}</text>")

    def box(self, x, y, w, h, lines, kind="step"):
        fill, stroke = KINDS[kind]
        rx = h / 2 if kind == "end" else 9
        width = 2 if kind == "end" else 1.4
        self.out.append(f'<rect x="{x - w / 2}" y="{y - h / 2}" width="{w}" height="{h}" rx="{rx}" '
                        f'style="fill:{fill};stroke:{stroke};stroke-width:{width}"/>')
        self._text(x, y, lines, w - 16)

    def ask(self, x, y, w, h, lines):
        """A decision: a diamond."""
        pts = f"{x},{y - h / 2} {x + w / 2},{y} {x},{y + h / 2} {x - w / 2},{y}"
        self.out.append(f'<polygon points="{pts}" style="fill:var(--card);stroke:var(--ink);'
                        'stroke-width:1.4"/>')
        self._text(x, y, lines, w * 0.66)

    def line(self, pts, label="", at=None, anchor="start", arrow=True, dashed=False):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        end = f' marker-end="url(#{self.ident}-a)"' if arrow else ""
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.out.append(f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="1.4"'
                        f"{end}{dash}/>")
        if label:
            x, y = at or (pts[0][0] + 6, pts[0][1] - 6)
            self.out.append(f'<text class="l" x="{x}" y="{y}" text-anchor="{anchor}">{e(label)}</text>')

    def note(self, x, y, text, anchor="start"):
        """A line's label, placed on its own (a second line of one, or one clear of a node)."""
        self.out.append(f'<text class="l" x="{x}" y="{y}" text-anchor="{anchor}">{e(text)}</text>')

    def svg(self, static: bool = False) -> str:
        marker = (f'<defs><marker id="{self.ident}-a" viewBox="0 0 10 10" refX="9" refY="5" '
                  'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                  '<path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>')
        body = "".join(self.out)
        if not static:
            return (f'<svg viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{e(self.label)}">'
                    f"{marker}{body}</svg>")
        # A file on its own: the page's colours and type are written in, on a white ground.
        body = re.sub(r"var\((--[a-z-]+)\)", lambda m: LIGHT[m.group(1)], body)
        body = (body.replace('class="b"', 'font-weight="650"')
                .replace('class="s"', 'font-size="11.5" opacity=".82"')
                .replace('class="l"', 'font-size="11.5" font-style="italic"'))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{e(self.label)}" '
                'font-family="-apple-system,system-ui,Helvetica,Arial,sans-serif" font-size="12.5" '
                f'fill="{LIGHT["--ink"]}" color="{LIGHT["--ink"]}">'
                f'<rect width="{self.w}" height="{self.h}" fill="#ffffff"/>{marker}{body}</svg>')

class Setup:
    """The config, read the way the figures need it: who fills each role, and what it is."""
    def __init__(self, cfg: dict):
        self.cfg, self.roles, self.backends = cfg, cfg["roles"], cfg["backends"]

    def name(self, role: str) -> str:
        """The backend in a role, if it is configured and switched on; else ""."""
        n = self.roles.get(role) or ""
        c = self.backends.get(n)
        return n if c and c.get("enabled", True) else ""

    def model(self, backend: str) -> str:
        c = self.backends.get(backend) or {}
        argv = c.get("argv", [])
        m = c.get("model") or (argv[argv.index("-m") + 1] if "-m" in argv[:-1] else "")
        m = m.rsplit("/", 1)[-1]
        if not m:
            m = next((a for a in argv if a in ("claude", "codex", "opencode")), "")
            m = {"claude": "Claude Code", "codex": "Codex", "opencode": "opencode"}.get(m, m)
        return m[:24]

    def kind(self, backend: str) -> str:
        c = self.backends.get(backend) or {}
        if c.get("type") == "openai_compat" or "opencode" in c.get("argv", []):
            return "local"
        return "api" if c.get("type") == "anthropic_api" else "plan"

    def tag(self, backend: str, room: int = 40) -> str:
        """`name (model)`, cut to the characters a box has room for."""
        name = backend[:16]
        m = self.model(backend)[:max(room - len(name) - 3, 0)]
        return f"{name} ({m})" if m else name

    def hours(self, backend: str) -> str:
        c = self.backends.get(backend) or {}
        if c.get("type") == "anthropic_api":
            return "tonight's API allowance"
        bits = []
        if c.get("window"):
            bits.append(f"{c['window'][0]:02d}:00-{c['window'][1]:02d}:00")
        if c.get("type") == "command":
            bits.append(f"{c.get('max_runs_per_night', 5)} runs a night")
        return ", ".join(bits) or "any hour"

def routing(s: Setup) -> Fig:
    f = Fig("wf1", 900, 470, "How a queued task reaches the written-answer path or the agent path")
    local = s.name("local") or "the local model"
    sizer = s.roles.get("classifier") or local
    f.box(450, 34, 320, 44, ["A task is queued", "from the CLI, the web page, a phone or SMS"], "end")
    f.box(450, 116, 400, 48, ["A run picks it up", "nightly, or within a minute of a scheduled time"])
    f.ask(450, 212, 220, 84, ["Which tier", "was chosen?"])
    if sizer == "off":
        f.box(450, 326, 320, 52, ["Not sized up", "the classifier is off: auto means an answer"])
    else:
        f.box(450, 326, 320, 52, ["It is sized up, once", f"{sizer[:18]} sorts it: tools, web or answer"],
              s.kind(sizer))
    f.box(170, 430, 300, 50, ["Written-answer path", "figure 2"], "end")
    f.box(730, 430, 300, 50, ["Agent path", "figure 3"], "end")
    f.line([(450, 56), (450, 91)])
    f.line([(450, 140), (450, 169)])
    f.line([(450, 254), (450, 299)], "auto", (458, 280))
    f.line([(340, 212), (100, 212), (100, 404)], "local or frontier", (330, 204), "end")
    f.line([(560, 212), (800, 212), (800, 404)], "agent", (570, 204))
    f.line([(400, 352), (400, 380), (240, 380), (240, 404)], "web or answer", (392, 373), "end")
    f.line([(500, 352), (500, 380), (660, 380), (660, 404)], "needs tools", (508, 373))
    return f

def answers(s: Setup) -> Fig:
    f = Fig("wf2", 900, 800, "How a written answer is produced, reviewed, retried and escalated")
    local, api, sub = s.name("local"), s.name("frontier_api"), s.name("frontier_sub")
    vision = s.name("local_vision")
    claude = api or sub
    second, stand_in = s.name("second_opinion"), s.name("stand_in")
    second, stand_in = (second, stand_in) if second else (stand_in, "")
    spare = s.name("local_review") or local
    f.box(300, 34, 280, 42, ["A written-answer task", "tier auto, local or frontier"], "end")
    f.ask(300, 128, 250, 90, ["Frontier tier, or", "needs the web?"])
    if claude:
        other = f"{sub[:18]} when the API allowance is gone" if api and sub else "no fallback is set up"
        f.box(700, 128, 300, 64, ["Claude answers, and can search the web" if api
                                  else "Claude answers", s.tag(claude), other], s.kind(claude))
    else:
        f.box(700, 128, 300, 64, ["It waits", "no Claude backend is set up"], "fail")
    f.ask(300, 250, 250, 90, ["Images or PDFs", "attached?"])
    if vision:
        f.box(700, 250, 300, 52, ["A local vision model reads them", s.tag(vision)], "local")
    else:
        f.box(700, 250, 300, 52, ["Claude reads them", "no local vision model is set up"],
              s.kind(claude) if claude else "fail")
    f.box(300, 366, 300, 52, ["The local model answers", s.tag(local) if local else "none set up"], "local")
    if stand_in:
        who = f"{second[:12]}, then {stand_in[:12]}, {spare[:12]}"
    elif second:
        who = f"{second[:16]}; {spare[:16]} if it can't"
    else:
        who = spare[:30]
    f.box(300, 456, 300, 52, ["A reviewer checks the answer", who],
          s.kind(second) if second else "local")
    f.ask(300, 552, 200, 80, ["PASS?"])
    f.box(700, 552, 240, 46, ["Done", "saved; in the morning digest"], "end")
    f.ask(300, 668, 230, 86, ["Rejected", "twice?"])
    if claude:
        f.box(700, 668, 300, 52, ["Claude takes the task over", s.tag(claude)], s.kind(claude))
    else:
        f.box(700, 668, 300, 52, ["The local model tries once more", "there is no Claude to hand it to"],
              "local")
    f.box(700, 762, 240, 46, ["Failed", "after 3 failed attempts"], "fail")
    f.line([(300, 55), (300, 82)])
    f.line([(425, 128), (549, 128)], "yes", (433, 120))
    f.line([(300, 173), (300, 204)], "no", (308, 193))
    f.line([(425, 250), (549, 250)], "yes", (433, 242))
    f.line([(300, 295), (300, 339)], "no", (308, 318))
    f.line([(300, 392), (300, 429)])
    f.line([(300, 482), (300, 511)])
    f.line([(400, 552), (579, 552)], "yes", (408, 544))
    f.line([(300, 592), (300, 624)], "no", (308, 612))
    f.line([(185, 668), (60, 668), (60, 366), (149, 366)], "no", (176, 660), "end")
    f.note(68, 500, "again, with")
    f.note(68, 515, "the feedback")
    f.line([(415, 668), (549, 668)], "yes", (423, 660))
    f.line([(700, 642), (700, 576)], "its answer is kept", (708, 612))
    f.line([(700, 694), (700, 738)], "if that fails too", (708, 720))
    f.line([(850, 128), (880, 128), (880, 552), (821, 552)], "not reviewed", (874, 400), "end")
    f.line([(850, 250), (880, 250)], arrow=False)
    return f

def agents(s: Setup) -> Fig:
    f = Fig("wf3", 920, 1010, "How an agent task is worked, checked, reviewed, retried and escalated")
    la, ca = s.name("local_agent"), s.name("agent")
    review, handoff = s.name("agent_review"), s.name("agent_handoff")
    stand_in = s.name("stand_in")
    if stand_in:  # it does a manager's job when that one can't, or isn't switched on
        review = review or (stand_in if s.roles.get("agent_review") else "")
        handoff = handoff or (stand_in if s.roles.get("agent_handoff") else "")
    lim = s.cfg["limits"]
    hard, after, most, outs = (lim["hard_priority"], lim["escalate_after"], lim["max_attempts"],
                               lim["max_timeouts"])
    f.box(450, 34, 320, 44, ["An agent task", "work that needs tools, in its own workspace"], "end")
    f.ask(450, 132, 260, 92, [f"Priority 1-{hard}, or", f"{after} failed attempt{'' if after == 1 else 's'}?"])
    if ca:
        f.box(775, 132, 260, 64, ["Claude's agent works on it", s.tag(ca, 36), s.hours(ca)], s.kind(ca))
    else:
        f.box(775, 132, 260, 64, ["No Claude agent is set up", "the local agent keeps the task"], "fail")
    if la:
        f.box(450, 248, 280, 58, ["The local agent works on it", s.tag(la), s.hours(la)], s.kind(la))
    else:
        f.box(450, 248, 280, 58, ["No local agent is set up", "every agent task goes to Claude's agent"])
    f.ask(450, 368, 250, 90, ["How did the", "run end?"])
    if handoff:
        f.box(140, 368, 250, 76, ["It ran out of time", f"{handoff[:18]} leaves a handoff note;",
                                  "the next run continues the session"], s.kind(handoff))
    else:
        f.box(140, 368, 250, 76, ["It ran out of time", "the next run continues the session",
                                  "(no manager set to leave a note)"])
    f.box(775, 368, 260, 76, ["It crashed, or the backend is down", "one attempt lost; it waits for the",
                              "next run. 3 errors rest a backend"], "fail")
    f.ask(450, 484, 250, 86, ["Did it end with", "STATUS: DONE?"])
    f.box(450, 588, 280, 52, ["night-elves runs its CHECK command", "in a sandbox with no network"])
    f.ask(450, 684, 220, 80, ["Did it exit 0?"])
    if review:
        f.box(450, 782, 280, 52, [f"{review[:18]} reads the workspace",
                                  f"{stand_in[:18]} if it can't" if stand_in not in ("", review)
                                  else "the local agent's work only"], s.kind(review))
    else:
        f.box(450, 782, 280, 52, ["No second reader", "roles.agent_review is not set"])
    f.ask(450, 878, 220, 80, ["PASS?"])
    f.box(450, 976, 330, 50, ["Done", "workspace committed; pushed to GitHub if asked"], "end")
    f.box(775, 684, 260, 76, ["A failed attempt", "the reason goes back to the agent,",
                              "which carries on in its session"], "fail")
    f.box(775, 976, 220, 50, ["Failed", f"after {most} failed attempts"], "fail")
    f.line([(450, 56), (450, 85)])
    f.line([(580, 132), (644, 132)], "yes", (586, 124))
    f.line([(450, 178), (450, 218)], "no", (458, 204))
    f.line([(450, 277), (450, 322)])
    f.line([(775, 164), (775, 300), (450, 300)], arrow=False)
    f.line([(325, 368), (266, 368)])
    f.line([(140, 330), (140, 132), (319, 132)])
    f.note(148, 236, "not a failed attempt")
    f.note(148, 251, f"({outs} time-outs fail it)")
    f.line([(575, 368), (644, 368)], "error", (581, 360))
    f.line([(450, 413), (450, 440)], "it replied", (458, 432))
    f.line([(450, 527), (450, 561)], "yes", (458, 549))
    f.line([(450, 614), (450, 643)])
    f.line([(450, 724), (450, 755)], "yes", (458, 744))
    f.line([(450, 808), (450, 837)])
    f.line([(450, 918), (450, 950)], "yes", (458, 938))
    f.line([(575, 484), (775, 484), (775, 645)], "no, or no STATUS line", (583, 476))
    f.line([(560, 684), (644, 684)], "no", (568, 676))
    f.line([(560, 878), (720, 878), (720, 723)], "FAIL", (568, 870))
    f.line([(830, 722), (830, 950)], f"the {_nth(most)} time", (838, 840))
    f.line([(905, 684), (912, 684), (912, 70), (450, 70)], "tries again", (906, 60), "end", arrow=False)
    return f

def _nth(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")

FAILURES = (
    ("A reviewer rejects an answer, or an agent's check fails",
     "A failed attempt. The reason is sent back and the task is tried again straight away; an "
     "agent continues its session. After {after} failed attempts Claude takes the task; after "
     "{most} it is marked failed."),
    ("An agent runs out of time",
     "Not a failed attempt: its files and session are kept, the others get their turn, and it "
     "continues. It counts towards moving to Claude's agent, and {outs} time-outs fail it."),
    ("A backend errors (the model server is down, a tool crashed)",
     "One attempt lost, and the task waits for the next run. Three errors in a row rest that "
     "backend for the remainder of the run; an agent that worked and then failed doesn't count."),
    ("No backend can take the task now",
     "It stays queued: outside a backend's hours, out of its runs for the night, or tonight's "
     "API allowance is spent. Nothing is lost and no attempt is counted."),
    ("The task reaches its API spending limit",
     "Failed at once, since trying again would be refused again ([budget] max_usd_per_task)."),
    ("A reviewer or the agent's manager can't answer",
     "The task carries on without it: roles.stand_in does the job if one is set, then the "
     "local reviewer for an answer; a manager's step nobody can do is skipped."),
    ("A task failed, or its result isn't what you wanted",
     "On the task's page: Run again, or add instructions and resubmit. Guide / fix has a "
     "frontier model read the run and write those instructions for you to edit."),
)

def figures(cfg: dict) -> list[tuple[Fig, str, str]]:
    """(figure, heading, caption) for the three flowcharts."""
    s = Setup(cfg)
    b = cfg["budget"]
    return [
        (routing(s), "1. From the queue to a path",
         "The tier you pick decides the path. An auto task is read once by a local model, which "
         "sorts it by what it needs; one that needs tools becomes an agent task. Tasks run one at "
         "a time: scheduled ones that are due first, then by priority, then oldest first."),
        (answers(s), "2. Written answers",
         "The local model answers and a second model reviews it. This is the first half of the "
         "billing cycle; in the second half Claude also plans and reviews local work, and in the "
         f"last {b['endgame_days']} days it answers priority 1-{cfg['limits']['hard_priority']} tasks "
         "itself, so that API credit about to expire is used."),
        (agents(s), "3. Agent tasks",
         "An agent is never taken at its word: it must end with STATUS: DONE and a CHECK command, "
         "night-elves runs that command itself, and a second model reads the local agent's work "
         "before it is accepted."),
    ]

def render(cfg: dict, links: bool = True) -> str:
    """The page. links: the full site (the view-only listener has no queue to go back to)."""
    lim = cfg["limits"]
    s = Setup(cfg)
    body = ["<div class=wf><div class=top style='margin-bottom:.4em'>",
            ("<a href=/>&larr; Queue</a> · <a href=/dashboard>Dashboard</a>" if links
             else "<a href=/>&larr; Dashboard</a>"), "</div>",
            "<h1>How a task is worked</h1>",
            "<p class=lead>What night-elves decides at each step, which model does the work, and "
            "what happens when a step fails. The boxes name the backends this Mac is set up to "
            "use right now.</p>",
            "<div class=legend>" + "".join(
                f"<span><i class=sw style='background:{KINDS[k][0]};border-color:{KINDS[k][1]}'></i>"
                f"{e(text)}</span>" for k, text in LEGEND) + "</div>"]
    for fig, heading, caption in figures(cfg):
        body.append(f"<h2>{e(heading)}</h2><figure><div class='card fig'>{fig.svg()}</div>"
                    f"<figcaption>{e(caption)}</figcaption></figure>")
    rows = "".join(
        f"<tr><td><b>{e(what)}</b></td><td>{e(then.format(after=lim['escalate_after'], most=lim['max_attempts'], outs=lim['max_timeouts']))}</td></tr>"
        for what, then in FAILURES)
    body.append("<h2>When something goes wrong</h2><div class='card scroll'><table><tr><th>What happens"
                f"<th>What night-elves does</tr>{rows}</table></div>")
    paid = {"local": "free", "plan": "flat-rate plan, not billed per call", "api": "billed per token"}
    used = []
    for role, text in dash_roles().items():
        n = s.name(role)
        if n:
            used.append(f"<tr><td><b>{e(role)}</b></td><td>{e(text)}</td><td>{e(s.tag(n))}</td>"
                        f"<td>{e(paid[s.kind(n)])}</td><td>{e(s.hours(n))}</td></tr>")
    body.append("<h2>Who does what on this Mac</h2><div class='card scroll'><table><tr><th>Role<th>Its job"
                f"<th>Backend (model)<th>Paid by<th>Held back by</tr>{''.join(used)}</table></div>"
                "<p class=hint>Roles are set under [roles] in config.toml; a role left empty is not "
                "listed.</p>")
    body.append(f"</div><style>{CSS}</style>")
    return pages.page("How a task is worked", "".join(body))

def dash_roles() -> dict:
    from .dash import ROLE_TEXT  # dash imports pages too; this keeps the import order simple
    return ROLE_TEXT

def example() -> dict:
    """The setup the README describes, for the figures saved as files."""
    from . import config
    from unittest import mock
    with mock.patch.object(config, "find_config", return_value=None):
        cfg = config.load()
    cfg["backends"] = {
        "local": {"type": "openai_compat", "model": "gpt-oss-120b"},
        "local_vision": {"type": "openai_compat", "model": "qwen3.8-27b"},
        "claude_api": {"type": "anthropic_api", "model": "claude-opus-5-5"},
        "claude_cli": {"type": "command", "argv": ["claude", "-p"], "window": [1, 5]},
        "claude_agent": {"type": "command", "argv": ["claude", "-p"], "window": [1, 7],
                         "max_runs_per_night": 3},
        "local_agent": {"type": "command", "argv": ["srt", "--", "opencode", "run", "-m",
                                                    "lmstudio/qwen3.8-27b"], "max_runs_per_night": 10},
        "codex": {"type": "command", "argv": ["srt", "--", "codex", "exec"], "max_runs_per_night": 30},
    }
    cfg["roles"].update(local_vision="local_vision", local_review="local_vision",
                        local_agent="local_agent", agent_review="codex", agent_handoff="codex")
    return cfg

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "assets")
    out.mkdir(parents=True, exist_ok=True)
    for fig, name in zip((f for f, _, _ in figures(example())), ("routing", "answers", "agents")):
        (out / f"workflow-{name}.svg").write_text(fig.svg(static=True) + "\n")
        print(out / f"workflow-{name}.svg")
