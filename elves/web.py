"""Web and SMS intake, plus phone pages to see and change the queue."""
from __future__ import annotations
import email.parser, email.policy, hashlib, hmac, html, io, json, os, re, shutil, time
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse
from . import attachments, db, pages, policy, when

MAX_BODY = 64 * 1024
MAX_FIELD = 1024 * 1024  # a non-file field of a multipart upload (the prompt)
CHUNK = 1024 * 1024
FILE_FIELDS = ("files", "images")  # "images" is what 0.4 Shortcuts send
BAD_TOKEN = {"error": "unauthorized", "message": "Not queued: the token was wrong"}  # Shortcuts
MAX_RESULT = 256 * 1024  # bytes of a result file shown on its page

AT = re.compile(rf"^at\s+({when.CLOCK})(?:\s*(?:cst|cdt|ct))?\s*[:,]?\s+", re.I)
FROM = re.compile(r"^from\s+(#\d+(?:\s*(?:,|and|&)\s*#\d+)*)\s*[:,]?\s+", re.I)

def parse_inline(text: str) -> tuple[str, int, str, str | None, list[int]]:
    """Phone shorthand, in any order at the start: '!' means priority 1, 'opus:' the frontier
    tier, 'agent:' the agent tier, 'at 1:30am' (a colon or am/pm is needed) not before then,
    and 'from #9' (or 'from #9, #12') attaches those tasks' results."""
    text, priority, tier, at, refs = text.strip(), 5, "auto", None, []
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
        elif m := FROM.match(text):
            text = text[m.end():]
            for n in map(int, re.findall(r"\d+", m.group(1))):
                if n not in refs:
                    refs.append(n)
        else:
            return text, priority, tier, at, refs

def run_after(at: str | None) -> str | None:
    return db.iso(when.parse_at(at, datetime.now().astimezone())) if at else None

def queued_msg(tid: int, after: str | None, uploads: int = 0, refs=()) -> str:
    parts = [f"{uploads} file{'s' if uploads > 1 else ''}"] if uploads else []
    if refs:
        parts.append(f"the result{'s' if len(refs) > 1 else ''} of "
                     + ", ".join(f"#{r}" for r in refs))
    return (f"Queued #{tid}" + (f" for {when.label(after)}" if after else "")
            + (" with " + " and ".join(parts) if parts else ""))

def queue(cfg: dict, conn, text: str, staged, refs, **kw) -> tuple[int, int]:
    """Queue from the web or SMS: uploads (if staged) plus earlier tasks' results.
    Returns (id, uploads kept)."""
    st = staged if staged is not None else attachments.Staged(cfg)
    try:
        tid, nfiles = attachments.queue(cfg, conn, text, st, refs, **kw)
    finally:
        if staged is None:
            st.discard()
    return tid, nfiles - len(refs)

COOKIE = "elves"
TASK_PATH = re.compile(r"^/(t|tasks)/(\d+)$")
FILE_PATH = re.compile(r"^/(t|tasks)/(\d+)/f/(\d+)$")
WORK_PATH = re.compile(r"^/t/(\d+)/w/(.+)$")  # an image an agent saved in its workspace

def read_multipart(rfile, length: int, boundary: str, new_file, parts: list | None = None) -> dict:
    """Stream a multipart/form-data body. File parts are written straight to the paths
    new_file(filename) returns, so a big upload never sits in memory; other fields come back
    as strings. With `parts`, a short description of each part is appended to it for the log."""
    if not boundary:
        raise ValueError("multipart body without a boundary")
    delim = b"--" + boundary.encode("latin-1")
    sep = b"\r\n" + delim
    buf, left, fields = b"", length, {}
    def fill() -> bool:
        nonlocal buf, left
        if left <= 0:
            return False
        data = rfile.read(min(CHUNK, left))
        if not data:
            raise ValueError("the upload ended early")
        left -= len(data)
        buf += data
        return True
    while (i := buf.find(delim)) < 0:
        if len(buf) > 65536 or not fill():
            raise ValueError("bad multipart body")
    buf = buf[i + len(delim):]
    while True:
        while len(buf) < 2 and fill():
            pass
        if buf.startswith(b"--"):
            return fields
        if not buf.startswith(b"\r\n"):
            raise ValueError("bad multipart body")
        buf = buf[2:]
        while (h := buf.find(b"\r\n\r\n")) < 0:
            if len(buf) > 16384 or not fill():
                raise ValueError("bad multipart headers")
        head = email.parser.BytesHeaderParser(policy=email.policy.HTTP).parsebytes(buf[:h + 4])
        buf = buf[h + 4:]
        name = head.get_param("name", header="content-disposition")
        filename = head.get_filename()
        is_file = filename is not None or name in FILE_FIELDS
        with (open(new_file(filename), "wb") if is_file else io.BytesIO()) as out:
            while (j := buf.find(sep)) < 0:
                keep = len(sep) - 1
                if len(buf) > keep:
                    out.write(buf[:-keep])
                    buf = buf[-keep:]
                if not is_file and out.tell() > MAX_FIELD:
                    raise ValueError(f"field {name!r} is too long")
                if not fill():
                    raise ValueError("the upload ended early")
            out.write(buf[:j])
            buf = buf[j + len(sep):]
            if parts is not None:
                parts.append(f"{name}({'file ' + repr(filename) if filename is not None else 'field'}"
                             f" {head.get_content_type()} {out.tell()}B)")
            if not is_file and name:
                fields[name] = out.getvalue().decode(errors="replace")
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
        r = policy.agent_route(t, cfg, lambda b: b in cfg["backends"]) if t["tier"] == "agent" else None
        window = cfg["backends"][r.executor].get("window") if r else None
        if window and not policy.in_window(window, now.hour):
            return f"#{tid} will start at {window[0]:02d}:00, when agent tasks may run"
        return f"#{tid} will start within a minute"
    if action != "save":
        raise ValueError(f"unknown action {action!r}")
    fields = {k: d[k] for k in ("prompt", "tier", "priority", "criteria") if k in d}
    if "priority" in fields:
        try:
            fields["priority"] = int(fields["priority"])
        except (TypeError, ValueError):
            raise ValueError("priority must be a number 1-9") from None
    if "at" in d:
        fields["run_after"] = run_after(str(d["at"] or "").strip() or None)
    if not fields:
        raise ValueError("nothing to change")
    db.change(conn, tid, vision=attachments.local_vision(cfg), **fields)
    return f"Saved #{tid}"

def task_json(t) -> dict:
    d = {k: t[k] for k in t.keys() if k != "images"}
    return {**d, "files": attachments.files(t)}

def make_handler(cfg: dict, token: str):
    # The browser cookie is derived from the token, so changing the token signs every browser out.
    session = hmac.new(token.encode(), b"night-elves web session", hashlib.sha256).hexdigest()
    results = Path(cfg["paths"]["results"]).expanduser().resolve()
    max_upload = int(cfg["web"].get("max_upload_mb", 2048)) * 2**20

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

        def _body(self, staged=None) -> dict:
            """Form fields or a JSON object; with `staged`, a multipart/form-data upload whose
            files are streamed into it."""
            n = int(self.headers.get("Content-Length") or 0)
            if staged is not None:
                if n > max_upload:
                    raise ValueError(f"the upload is over {max_upload // 2**20} MB")
                return read_multipart(self.rfile, n, self.headers.get_param("boundary") or "",
                                      staged.new_file, self.parts)  # queue() finishes it
            if n > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(n).decode(errors="replace")
            self.parts.append(f"{self.headers.get_content_type()} {n}B")
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

        def _file(self, t, n: int):
            """One attached file. Only images, PDFs and plain text open in the browser;
            everything else (HTML and SVG included) downloads, so an upload can't run as
            part of this site."""
            fs, ps = attachments.files(t), attachments.paths(cfg, t)
            if not 1 <= n <= len(fs) or not ps[n - 1].is_file():
                return self._send(404, {"error": "no such file"})
            f, p = fs[n - 1], ps[n - 1]
            ext = p.suffix.lstrip(".").lower()
            ctype, disp = {"image": (attachments.TYPES.get(ext), "inline"),
                           "pdf": ("application/pdf", "inline"),
                           "text": ("text/plain; charset=utf-8", "inline")}.get(f["kind"], (None, ""))
            if not ctype:
                ctype, disp = "application/octet-stream", "attachment"
            self.send_response(200)
            for k, v in (("Content-Type", ctype), ("Content-Length", str(p.stat().st_size)),
                         ("Content-Disposition", f'{disp}; filename="{p.name}"'),
                         ("Cache-Control", "private, max-age=3600"),
                         ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, CHUNK)

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

        def _work_image(self, tid: int, rel: str):
            p = attachments.work_image(cfg, tid, unquote(rel))
            if p is None:
                return self._send(404, {"error": "no such file"})
            ext = p.suffix.lstrip(".").lower()
            self._send(200, p.read_bytes(), attachments.TYPES.get(ext, "image/jpeg"),
                       [("Cache-Control", "private, max-age=60"),
                        ("X-Content-Type-Options", "nosniff")])

        def do_GET(self):
            u = urlparse(self.path)
            msg = parse_qs(u.query).get("m", [None])[0]
            if w := WORK_PATH.match(u.path):
                if not self._cookie_ok():
                    return self._redirect("/")
                return self._work_image(int(w.group(1)), w.group(2))
            m = TASK_PATH.match(u.path) or FILE_PATH.match(u.path)
            if u.path in ICON_PATHS:  # no sign-in: iOS fetches it when adding to the Home Screen
                return self._send(200, ICON.read_bytes(), "image/png",
                                  [("Cache-Control", "public, max-age=86400")])
            if u.path == "/":
                if not self._cookie_ok():
                    return self._send(200, pages.login(), HTML)
                conn = db.connect(cfg["paths"]["db"])
                try:
                    rows = db.list_tasks(conn, limit=300)
                    return self._send(200, pages.home(rows, msg, db.usage(conn, [t["id"] for t in rows])), HTML)
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
                if m.re is FILE_PATH:
                    return self._file(t, int(m.group(3)))
                use = db.usage(conn, [t["id"]]).get(t["id"])
                if m.group(1) == "tasks":
                    return self._send(200, {**task_json(t), "result": self._result(t), "usage": use})
                raw = parse_qs(u.query).get("raw", [""])[0] == "1"
                shots = attachments.work_images(cfg, t["id"]) if t["tier"] == "agent" else []
                return self._send(200, pages.task(t, self._result(t), msg, raw, shots, use), HTML)
            finally:
                conn.close()

        def do_POST(self):
            u = urlparse(self.path)
            if not (self._multipart() and u.path == "/tasks"):
                return self._post(u, None)
            if not self._authed():  # before reading what may be a big upload
                self.close_connection = True
                return self._send(401, BAD_TOKEN)
            with attachments.Staged(cfg) as staged:
                return self._post(u, staged)

        def _post(self, u, staged):
            self.parts = []
            try:
                d = self._body(staged)
            except (ValueError, OSError) as e:
                self.close_connection = True  # the rest of the body may still be unread
                if staged is not None and not self._wants_json():
                    return self._redirect("/?m=" + quote(f"Not queued: {e}"))
                return self._send(400, {"error": str(e), "message": f"Not queued: {e}"})
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
                        return self._send(401, BAD_TOKEN)
                    try:
                        text, priority, tier, at, refs = parse_inline(str(d.get("prompt", "")))
                        refs = attachments.parse_refs([*refs, d.get("from") or ""])
                        if d.get("tier") not in (None, "", "auto"):
                            tier = d["tier"]
                        after = run_after(d.get("at") or at)
                        kw = dict(source="web", tier=tier, run_after=after,
                                  priority=int(d.get("priority") or priority),
                                  criteria=str(d.get("criteria") or ""))
                        tid, nfiles = queue(cfg, conn, text, staged, refs, **kw)
                    except (ValueError, OSError) as e:
                        if self._wants_json():
                            return self._send(400, {"error": str(e), "message": f"Not queued: {e}"})
                        return self._redirect("/?m=" + quote(f"Not queued: {e}"))
                    msg = queued_msg(tid, after, nfiles, refs)
                    named = staged is not None and any(n for _, n in staged.incoming)
                    if not nfiles and (named or any(d.get(k) for k in FILE_FIELDS)):
                        msg += ", but no file came through"  # sent as text, or empty
                    print(f"intake #{tid}: {', '.join(self.parts) or '-'}; "
                          f"fields {sorted(d)}; {nfiles} file(s) kept", flush=True)
                    if not self._wants_json():
                        return self._redirect("/?m=" + quote(msg))
                    return self._send(201, {"id": tid, "status": "queued", "run_after": after,
                                            "files": nfiles, "message": msg})
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
                    text, priority, tier, at, refs = parse_inline(d.get("Body", ""))
                    after = run_after(at)
                    try:
                        tid, _ = queue(cfg, conn, text, None, refs, source="sms",
                                       priority=priority, tier=tier, run_after=after)
                        msg = html.escape(queued_msg(tid, after, refs=refs))
                    except ValueError as e:
                        msg = html.escape(f"Not queued: {e}")
                    return self._send(200, f"<Response><Message>{msg}</Message></Response>",
                                      "text/xml")
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            finally:
                conn.close()

        def log_message(self, fmt, *args):
            line = (fmt % args).split("?")[0]  # never log the key in a query string
            print(f"{self.address_string()} {line}", flush=True)
    return Handler

def serve(cfg: dict) -> None:
    token = os.environ.get(cfg["web"]["token_env"], "")
    if len(token) < 16:
        raise SystemExit(f"Set {cfg['web']['token_env']} to a secret of at least 16 characters.")
    addr = (cfg["web"]["host"], cfg["web"]["port"])
    print(f"night-elves intake on http://{addr[0]}:{addr[1]}")
    ThreadingHTTPServer(addr, make_handler(cfg, token)).serve_forever()
