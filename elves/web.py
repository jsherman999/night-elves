"""Web and SMS intake. All it does is insert rows into the queue."""
from __future__ import annotations
import hmac, html, json, os, re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from . import db, when

MAX_BODY = 64 * 1024
FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>night-elves</title><style>body{font:16px system-ui;max-width:34em;margin:2em auto;padding:0 1em}
textarea,input,select,button{font:inherit;width:100%;margin:.3em 0;box-sizing:border-box}</style>
<h1>night-elves</h1><form method=post action=/tasks>
<textarea name=prompt rows=6 placeholder="What should the elves do tonight?" required></textarea>
<select name=tier><option>auto<option>local<option>frontier<option>agent</select>
<input name=priority type=number min=1 max=9 value=5>
<input name=at placeholder="run at (optional), e.g. 01:30 or 2pm">
<input name=token type=password placeholder=token required><button>Queue it</button></form>"""

AT = re.compile(rf"^at\s+({when.CLOCK})(?:\s*(?:cst|cdt|ct))?\s*[:,]?\s+", re.I)

def parse_inline(text: str) -> tuple[str, int, str, str | None]:
    """Phone shorthand, in any order at the start: '!' means priority 1, 'opus:' the frontier
    tier, 'agent:' the agent tier, and 'at 1:30am' (a colon or am/pm is needed) not before then."""
    text, priority, tier, at = text.strip(), 5, "auto", None
    while True:
        low = text.lower()
        if text.startswith("!"):
            text, priority = text[1:].lstrip(), 1
        elif low.startswith("opus:"):
            text, tier = text[5:].lstrip(), "frontier"
        elif low.startswith("agent:"):
            text, tier = text[6:].lstrip(), "agent"
        elif (m := AT.match(text)) and (m.group(3) or m.group(4)):
            text, at = text[m.end():], m.group(1).strip()
        else:
            return text, priority, tier, at

def run_after(at: str | None) -> str | None:
    return db.iso(when.parse_at(at, datetime.now().astimezone())) if at else None

def queued_msg(tid: int, after: str | None) -> str:
    return f"Queued #{tid}" + (f" for {when.label(after)}" if after else "")

def make_handler(cfg: dict, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, supplied: str | None) -> bool:
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return any(s and hmac.compare_digest(s.encode(), token.encode())
                       for s in (bearer, supplied))

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(n).decode(errors="replace")
            if "json" in self.headers.get("Content-Type", ""):
                d = json.loads(raw or "{}")
                if not isinstance(d, dict):
                    raise ValueError("expected a JSON object")
                return d
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, FORM, "text/html; charset=utf-8")
            if u.path == "/tasks":
                if not self._authed(None):
                    return self._send(401, {"error": "unauthorized"})
                conn = db.connect(cfg["paths"]["db"])
                rows = [dict(r) for r in db.list_tasks(conn, limit=100)]
                conn.close()
                return self._send(200, rows)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                d = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            conn = db.connect(cfg["paths"]["db"])
            try:
                if u.path == "/tasks":
                    if not self._authed(d.get("token")):
                        return self._send(401, {"error": "unauthorized"})
                    text, priority, tier, at = parse_inline(str(d.get("prompt", "")))
                    if d.get("tier") not in (None, "", "auto"):
                        tier = d["tier"]
                    after = run_after(d.get("at") or at)
                    tid = db.add_task(conn, text, source="web", tier=tier, run_after=after,
                                      priority=int(d.get("priority") or priority))
                    return self._send(201, {"id": tid, "status": "queued", "run_after": after,
                                            "message": queued_msg(tid, after)})
                if u.path == "/sms":  # Twilio-style webhook: form fields From and Body
                    key = parse_qs(u.query).get("key", [None])[0]
                    if not self._authed(key) or d.get("From") not in cfg["web"]["sms_allow"]:
                        return self._send(401, {"error": "unauthorized"})
                    text, priority, tier, at = parse_inline(d.get("Body", ""))
                    after = run_after(at)
                    tid = db.add_task(conn, text, source="sms", priority=priority, tier=tier,
                                      run_after=after)
                    msg = html.escape(queued_msg(tid, after))
                    return self._send(200, f"<Response><Message>{msg}</Message></Response>",
                                      "text/xml")
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            finally:
                conn.close()

        def log_message(self, fmt, *args):
            line = (fmt % args).split("?")[0]  # never log the key in a query string
            print(f"{self.address_string()} {line}")
    return Handler

def serve(cfg: dict) -> None:
    token = os.environ.get(cfg["web"]["token_env"], "")
    if len(token) < 16:
        raise SystemExit(f"Set {cfg['web']['token_env']} to a secret of at least 16 characters.")
    addr = (cfg["web"]["host"], cfg["web"]["port"])
    print(f"night-elves intake on http://{addr[0]}:{addr[1]}")
    ThreadingHTTPServer(addr, make_handler(cfg, token)).serve_forever()
