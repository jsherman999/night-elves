"""Web and SMS intake, plus phone pages to see and change the queue."""
from __future__ import annotations
import email.parser, email.policy, hashlib, hmac, html, json, os, re, time
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from . import attachments, db, pages, policy, when

MAX_BODY = 64 * 1024
MAX_UPLOAD = 48 * 1024 * 1024  # a task with images, sent as multipart/form-data
MAX_RESULT = 256 * 1024  # bytes of a result file shown on its page

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

COOKIE = "elves"
TASK_PATH = re.compile(r"^/(t|tasks)/(\d+)$")
IMG_PATH = re.compile(r"^/(t|tasks)/(\d+)/img/(\d+)$")
HTML = "text/html; charset=utf-8"
ICON = Path(__file__).with_name("icon.png")  # source: assets/icon.svg
ICON_PATHS = {"/icon.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"}
CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")

def act(cfg: dict, conn, tid: int, d: dict) -> str:
    """One change to a task from the web: save (edit fields), now, cancel or retry."""
    action = d.get("action") or "save"
    if action == "cancel":
        db.cancel(conn, tid)
        return f"Cancelled #{tid}"
    if action == "retry":
        db.requeue(conn, tid)
        return f"#{tid} is back in the queue"
    if action == "now":
        db.change(conn, tid, ("queued",), run_after=db.iso())
        t, now = db.get(conn, tid), datetime.now()
        back = cfg["roles"].get("agent") if t["tier"] == "agent" else None
        window = cfg["backends"].get(back, {}).get("window") if back else None
        if window and not policy.in_window(window, now.hour):
            return f"#{tid} will start at {window[0]:02d}:00, when agent tasks may run"
        return f"#{tid} will start within a minute"
    if action != "save":
        raise ValueError(f"unknown action {action!r}")
    fields = {k: d[k] for k in ("prompt", "tier", "priority") if k in d}
    if "priority" in fields:
        try:
            fields["priority"] = int(fields["priority"])
        except (TypeError, ValueError):
            raise ValueError("priority must be a number 1-9") from None
    if "at" in d:
        fields["run_after"] = run_after(str(d["at"] or "").strip() or None)
    if not fields:
        raise ValueError("nothing to change")
    db.change(conn, tid, **fields)
    return f"Saved #{tid}"

def task_json(t) -> dict:
    return {**dict(t), "images": attachments.names(t)}

def make_handler(cfg: dict, token: str):
    # The browser cookie is derived from the token, so changing the token signs every browser out.
    session = hmac.new(token.encode(), b"night-elves web session", hashlib.sha256).hexdigest()
    results = Path(cfg["paths"]["results"]).expanduser().resolve()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json", headers=()):
            data = (body if isinstance(body, bytes) else body.encode() if isinstance(body, str)
                    else json.dumps(body).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if not any(k == "Cache-Control" for k, _ in headers):
                self.send_header("Cache-Control", "no-store")
            if ctype == HTML:
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where, cookie=None):
            self._send(303, "", "text/plain", [("Location", where)] + ([("Set-Cookie", cookie)] if cookie else []))

        def _token_ok(self, supplied: str | None) -> bool:
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return any(s and hmac.compare_digest(s.encode(), token.encode())
                       for s in (bearer, supplied))

        def _cookie_ok(self) -> bool:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except Exception:
                return False
            m = jar.get(COOKIE)
            return bool(m) and hmac.compare_digest(m.value.encode(), session.encode())

        def _same_origin(self) -> bool:
            """Browsers send Origin on form posts; refuse ones from another site. SameSite=Strict
            on the cookie already stops this; this is the second lock."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")} - {None}
            return urlparse(origin).netloc in hosts

        def _authed(self, supplied=None) -> bool:
            return self._token_ok(supplied) or (self._cookie_ok() and self._same_origin())

        def _cookie(self, value: str, max_age: int) -> str:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            secure = "" if host in ("127.0.0.1", "localhost", "::1") else "; Secure"
            return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

        def _multipart(self) -> bool:
            return self.headers.get_content_type() == "multipart/form-data"

        def _body(self) -> dict:
            """Form fields, a JSON object, or (POST /tasks only) multipart/form-data, where
            the files of the `images` field come back as a list of bytes under "images"."""
            n = int(self.headers.get("Content-Length") or 0)
            upload = self._multipart() and urlparse(self.path).path == "/tasks"
            if n > (MAX_UPLOAD if upload else MAX_BODY):
                raise ValueError("body too large" + (" (images too big)" if upload else ""))
            if upload:
                head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    head + self.rfile.read(n))
                if not msg.is_multipart():
                    raise ValueError("bad multipart body")
                d: dict = {"images": []}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    data = part.get_payload(decode=True) or b""
                    if name == "images":
                        d["images"].append(data)
                    elif name:
                        d[name] = data.decode(errors="replace")
                return d
            raw = self.rfile.read(n).decode(errors="replace")
            if self._json():
                d = json.loads(raw or "{}")
                if not isinstance(d, dict):
                    raise ValueError("expected a JSON object")
                return d
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _json(self) -> bool:
            return "json" in self.headers.get("Content-Type", "")

        def _wants_json(self) -> bool:
            """Scripts and Shortcuts (JSON bodies, a bearer token, or Accept: JSON) get JSON;
            browsers get redirects back to the pages."""
            return (self._json() or "application/json" in self.headers.get("Accept", "")
                    or self.headers.get("Authorization", "").startswith("Bearer "))

        def _image(self, t, n: int):
            files = attachments.paths(cfg, t)
            if not 1 <= n <= len(files) or not files[n - 1].is_file():
                return self._send(404, {"error": "no such image"})
            p = files[n - 1]
            return self._send(200, p.read_bytes(), attachments.TYPES[p.suffix.lstrip(".")],
                              [("Cache-Control", "private, max-age=86400"),
                               ("X-Content-Type-Options", "nosniff")])

        def _result(self, t) -> str | None:
            if not t["result_path"]:
                return None
            p = Path(t["result_path"]).resolve()
            if not p.is_relative_to(results) or not p.is_file():
                return None
            with open(p, errors="replace") as f:
                text = f.read(MAX_RESULT + 1)
            return text[:MAX_RESULT] + ("\n\n[cut short; the rest is in the file]"
                                        if len(text) > MAX_RESULT else "")

        def do_GET(self):
            u = urlparse(self.path)
            msg = parse_qs(u.query).get("m", [None])[0]
            m = TASK_PATH.match(u.path) or IMG_PATH.match(u.path)
            if u.path in ICON_PATHS:  # no sign-in: iOS fetches it when adding to the Home Screen
                return self._send(200, ICON.read_bytes(), "image/png",
                                  [("Cache-Control", "public, max-age=86400")])
            if u.path == "/":
                if not self._cookie_ok():
                    return self._send(200, pages.login(), HTML)
                conn = db.connect(cfg["paths"]["db"])
                try:
                    return self._send(200, pages.home(db.list_tasks(conn, limit=300), msg), HTML)
                finally:
                    conn.close()
            if u.path == "/tasks" or (m and m.group(1) == "tasks"):
                if not self._token_ok(None):
                    return self._send(401, {"error": "unauthorized"})
            elif m:
                if not self._cookie_ok():
                    return self._redirect("/")
            else:
                return self._send(404, {"error": "not found"})
            conn = db.connect(cfg["paths"]["db"])
            try:
                if u.path == "/tasks":
                    return self._send(200, [task_json(r) for r in db.list_tasks(conn, limit=100)])
                t = db.get(conn, int(m.group(2)))
                if t is None:
                    return self._send(404, {"error": "no such task"})
                if m.re is IMG_PATH:
                    return self._image(t, int(m.group(3)))
                if m.group(1) == "tasks":
                    return self._send(200, {**task_json(t), "result": self._result(t)})
                raw = parse_qs(u.query).get("raw", [""])[0] == "1"
                return self._send(200, pages.task(t, self._result(t), msg, raw), HTML)
            finally:
                conn.close()

        def do_POST(self):
            u = urlparse(self.path)
            if self._multipart() and u.path == "/tasks" and not self._authed():
                return self._send(401, {"error": "unauthorized"})  # before reading the upload
            try:
                d = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if u.path == "/login":
                if self._same_origin() and self._token_ok(str(d.get("token", ""))):
                    return self._redirect("/", self._cookie(session, 365 * 86400))
                time.sleep(1)  # slows guessing; the token is long anyway
                return self._send(401, pages.login("That token didn't match."), HTML)
            if u.path == "/logout":
                return self._redirect("/", self._cookie("", 0) if self._same_origin() else None)
            m = TASK_PATH.match(u.path)
            conn = db.connect(cfg["paths"]["db"])
            try:
                if u.path == "/tasks":
                    if not self._authed(d.get("token")):
                        return self._send(401, {"error": "unauthorized"})
                    try:
                        text, priority, tier, at = parse_inline(str(d.get("prompt", "")))
                        if d.get("tier") not in (None, "", "auto"):
                            tier = d["tier"]
                        after = run_after(d.get("at") or at)
                        kw = dict(source="web", tier=tier, run_after=after,
                                  priority=int(d.get("priority") or priority))
                        blobs = [b for b in d.get("images") or [] if b]
                        tid = (attachments.add_task(cfg, conn, text, blobs, **kw) if blobs
                               else db.add_task(conn, text, **kw))
                    except ValueError as e:
                        if self._wants_json():
                            return self._send(400, {"error": str(e)})
                        return self._redirect("/?m=" + quote(f"Not queued: {e}"))
                    msg = queued_msg(tid, after) + (f" with {len(blobs)} image"
                                                    f"{'s' if len(blobs) > 1 else ''}" if blobs else "")
                    if not self._wants_json():
                        return self._redirect("/?m=" + quote(msg))
                    return self._send(201, {"id": tid, "status": "queued", "run_after": after,
                                            "images": len(blobs), "message": msg})
                if m and m.group(1) == "tasks":
                    if not self._authed(d.get("token")):
                        return self._send(401, {"error": "unauthorized"})
                    tid = int(m.group(2))
                    try:
                        msg, code = act(cfg, conn, tid, d), 200
                    except LookupError as e:
                        msg, code = str(e).strip("'\""), 404
                    except ValueError as e:
                        msg, code = str(e), 409 if "can't be changed" in str(e) else 400
                    if not self._wants_json():
                        return self._redirect(f"/t/{tid}?m=" + quote(msg))
                    t = db.get(conn, tid)
                    return self._send(code, {"message": msg, **({"task": task_json(t)} if t else {})})
                if u.path == "/sms":  # Twilio-style webhook: form fields From and Body
                    key = parse_qs(u.query).get("key", [None])[0]
                    if not self._token_ok(key) or d.get("From") not in cfg["web"]["sms_allow"]:
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
