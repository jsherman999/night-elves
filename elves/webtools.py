"""Web search and page reading for local models.

Claude's API runs its own search and fetch on Anthropic's servers. A local model has neither,
so night-elves does it here: a search asks a SearXNG instance (search_url on the backend) and
opening a page downloads it and reduces it to text. research.py drives these in stages; the
model only ever gets text back.

The URLs come from search results, from pages, and from a model that has been reading pages,
so fetching is held to the public internet: http(s) only, and every host, redirects included,
must resolve to public addresses. That keeps a page from steering a fetch into this Mac's own
services or the LAN."""
from __future__ import annotations
import gzip, ipaddress, json, os, re, socket, time, urllib.error, urllib.parse, urllib.request, zlib
from html.parser import HTMLParser

AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) night-elves/1.0"
MAX_BYTES = 3 * 2**20   # of one page
TIMEOUT = 30
RESULTS = 8
ALLOW_PRIVATE = False   # tests fetch from a server on localhost

class Refused(ValueError):
    pass

class NotText(Refused):
    """The URL is there, but it is a file (an archive, a PDF), not a page of text."""

URL = re.compile(r"https?://[^\s<>\"'`\]\)\\|]+")

def urls_in(text: str) -> list[str]:
    """The http(s) URLs written in a text, in order, without trailing punctuation."""
    out = []
    for u in URL.findall(text):
        u = u.rstrip(".,;:!?*_'\u201d\u2019")
        if u not in out and len(u) > 10:
            out.append(u)
    return out

def norm(url: str) -> str:
    """A URL reduced for comparing: no fragment, no trailing slash, host in lower case, and
    http and https, with and without www, taken as the same."""
    u = urllib.parse.urlsplit(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return host + port + (u.path.rstrip("/") or "") + (f"?{u.query}" if u.query else "")

def check_url(url: str) -> str:
    """The URL, if it is http(s) to a host on the public internet."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused("only http and https URLs can be fetched")
    if ALLOW_PRIVATE:
        return u.geturl()
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except OSError as e:
        raise Refused(f"can't resolve {u.hostname}: {e}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise Refused(f"{u.hostname} is not on the public internet")
    if not infos:
        raise Refused(f"can't resolve {u.hostname}")
    return u.geturl()

class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "select"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
         "header", "footer", "pre", "blockquote", "table", "ul", "ol", "dt", "dd", "title", "hr"}

class _Text(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.skip, self.href, self.link = base, [], 0, None, []
    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in BLOCK:
            self.out.append("\n")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            full = urllib.parse.urljoin(self.base, href.strip())
            self.href = full if full.startswith(("http://", "https://")) else None
            self.link = []
    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(self.skip - 1, 0)
        elif tag == "a" and self.href is not None:
            text = " ".join("".join(self.link).split())
            self.out.append(f"[{text}]({self.href})" if text else "")
            self.href = None
        elif tag in BLOCK or tag in ("td", "th"):
            self.out.append("\n" if tag in BLOCK else " | ")
    def handle_data(self, data):
        if self.skip:
            return
        (self.link if self.href is not None else self.out).append(data)

def html_text(html: str, base: str = "") -> str:
    p = _Text(base)
    try:
        p.feed(html)
        p.close()
    except Exception:  # broken markup: keep what was read
        pass
    if p.href is not None:
        p.out.append("".join(p.link))
    lines = (" ".join(l.split()) for l in "".join(p.out).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def page_text(url: str, kind: bool = False):
    """The text of a public page. Raises Refused or OSError with a reason fit for the model.
    With kind, returns (text, whether it was HTML)."""
    req = urllib.request.Request(check_url(url), headers={
        "User-Agent": AGENT, "Accept": "text/html,text/plain,application/json,*/*;q=0.5"})
    opener = urllib.request.build_opener(_Redirects)
    with opener.open(req, timeout=TIMEOUT) as r:
        ctype = r.headers.get_content_type()
        if not (ctype.startswith("text/") or ctype in ("application/json", "application/xml",
                                                        "application/xhtml+xml",
                                                        "application/rss+xml",
                                                        "application/atom+xml")):
            size = r.headers.get("Content-Length")
            raise NotText(f"a {ctype} file" + (f" of {int(size):,} bytes" if size and
                          size.isdigit() else ""))
        raw = r.read(MAX_BYTES + 1)[:MAX_BYTES]
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc in ("gzip", "deflate"):
            try:
                raw = gzip.decompress(raw) if enc == "gzip" else zlib.decompress(raw)
            except (OSError, zlib.error, EOFError):
                pass
        text = raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
        final = r.geturl()
    out = html_text(text, final) if "html" in ctype else text.strip()
    return (out, "html" in ctype) if kind else out

def search(search_url: str, query: str, timeout: int = TIMEOUT) -> list[dict]:
    """Results from a SearXNG instance (its JSON output must be enabled), best first:
    [{"title", "url", "snippet"}]."""
    url = search_url.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        hits = json.loads(r.read()).get("results") or []
    return [{"title": " ".join(str(h.get("title") or "").split()),
             "url": str(h["url"]),
             "snippet": " ".join(str(h.get("content") or "").split())[:300]}
            for h in hits[:RESULTS] if str(h.get("url") or "").startswith(("http://", "https://"))]

# What the plain fetch says when a browser might do better: the site turned a script away,
# or the page is drawn by JavaScript. A page that is gone (404, 410) is not worth a browser.
BROWSER_HTTP = {401, 403, 406, 409, 429, 451, 503}
THIN = 400  # characters: an HTML page with less text than this was probably not rendered
# A site's bot check answers a headless browser too, with a short page like these.
WALL = re.compile(r"security verification|prove your humanity|verify(ing)? (that )?you are (a )?human|"
                  r"are you a robot|unusual traffic|captcha|access denied|403 forbidden|"
                  r"enable javascript|checking your browser", re.I)
READ_GOAL = ("Read this page. If a cookie, consent, sign-up or app banner covers the content, "
             "dismiss it. Do not follow links, search, log in or submit forms. Stop as soon as "
             "the page's main content is visible.")

class Reader:
    """A Jev browser service (reader_url on the backend) that reads the pages the plain fetch
    can't: it opens the page in a real, headless browser, gets past a consent banner, and
    returns the rendered text and links. Slow (up to `timeout` seconds a page, one at a time)
    and it spends a little model credit, so a task gets at most `pages` of these.

    It gets pages that are drawn by JavaScript. It does not get past a site's bot check
    (Cloudflare and the like turn a headless browser away as well): those reads are refused
    here, so a "prove you are human" page is never handed to the model as the page.

    The browser is not held to the public internet the way the plain fetch is, so the URL is
    checked before it is sent, and a read that passed through any non-public address is
    thrown away. That keeps a private page's text from the model; it can't stop the browser
    making the request."""
    def __init__(self, url: str, token: str = "", pages: int = 4, timeout: int = 90):
        self.url, self.token, self.left, self.timeout = url.rstrip("/"), token, pages, timeout
        self.misses = 0  # failures in a row: after three the service is left alone

    @classmethod
    def of(cls, c: dict) -> "Reader | None":
        if not c.get("reader_url"):
            return None
        return cls(c["reader_url"], os.environ.get(c.get("reader_token_env", "ELVES_READER_TOKEN"), ""),
                   int(c.get("reader_pages", 4)), int(c.get("reader_timeout", 90)))

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.url + path, method=method, data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # it is local
        with opener.open(req, timeout=15) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    def available(self) -> bool:
        return self.left > 0 and self.misses < 3

    def read(self, url: str) -> str:
        """The page's rendered text, with its links listed after it. Raises Refused or OSError."""
        self.left -= 1
        self.misses += 1
        job = self._call("POST", "/v1/jobs", {
            "url": check_url(url), "goal": READ_GOAL, "max_steps": 6, "isolate_browser": True,
            "timeout_seconds": self.timeout, "max_text_chars": 100000})
        jid, end = job["id"], time.monotonic() + self.timeout + 30  # its own limit, and a margin
        try:
            while job.get("status") in ("queued", "running"):
                if time.monotonic() > end:
                    self._call("POST", f"/v1/jobs/{jid}/cancel")
                    raise Refused("the browser didn't finish in time")
                time.sleep(2)
                job = self._call("GET", f"/v1/jobs/{jid}")
            res = job.get("result") or {}
            page = res.get("page") or {}
            text = (page.get("text") or "").strip()
            if not text:
                raise Refused(f"the browser got nothing ({job.get('status')}: "
                              f"{str(job.get('error') or 'no text on the page')[:120]})")
            for u in [*(res.get("visited_urls") or []), page.get("url") or url]:
                try:
                    check_url(u)
                except Refused:
                    raise Refused("the browser was taken to an address that is not on the "
                                  "public internet, so what it read was thrown away") from None
            if len(text) < THIN or (len(text) < 2000 and WALL.search(text)):
                raise Refused("the site's bot check stopped the browser too" if WALL.search(text)
                              else f"the browser got only {len(text)} characters of text")
            links, seen = [], set()
            for l in page.get("links") or []:
                u = str(l.get("url") or "")
                if u.startswith(("http://", "https://")) and u not in seen and len(links) < 200:
                    seen.add(u)
                    links.append(f"- [{' '.join(str(l.get('text') or '').split())[:120]}]({u})")
            self.misses = 0
            return text + ("\n\nLinks on the page:\n" + "\n".join(links) if links else "")
        finally:
            try:  # the service keeps what it captured until told to drop it
                self._call("DELETE", f"/v1/jobs/{jid}")
            except (OSError, ValueError):
                pass

def why(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"no answer ({e.reason})"
    return str(e) or type(e).__name__

class Session:
    """One task's searching and reading. Keeps what was searched for, every page opened (so
    nothing is fetched twice), what couldn't be opened and why, and every URL that appeared in
    a search result or on a page: the record that answers are checked against."""
    def __init__(self, search_url: str, reader: Reader | None = None):
        self.search_url, self.reader = search_url, reader
        self.browsed: dict[str, str] = {}  # url -> why the plain fetch failed, for pages the
                                           # browser read instead
        self.searches: list[str] = []
        self.pages: dict[str, str] = {}    # url -> text, for pages that opened
        self.errors: dict[str, str] = {}   # url -> why it didn't
        self.files: dict[str, str] = {}    # url -> what kind of file is there
        self.seen: set[str] = set()        # norm() of every URL met in results and pages

    def search(self, query: str) -> list[dict]:
        self.searches.append(query)
        try:
            hits = search(self.search_url, query)
        except (OSError, ValueError) as e:
            self.errors[f"search: {query}"] = why(e)
            return []
        self.seen.update(norm(h["url"]) for h in hits)
        return hits

    def _browse(self, url: str, reason: str) -> str | None:
        """Try the page in the browser, when there is one and this task has reads left."""
        if not (self.reader and self.reader.available()):
            return None
        try:
            text = self.reader.read(url)
        except (Refused, ValueError, KeyError, OSError) as e:
            self.errors[url] = f"{reason}; in the browser: {why(e)}"
            return None
        self.browsed[url] = reason
        return text

    def open(self, url: str, browse: bool = True) -> str | None:
        """The page's text, or None if it couldn't be read (errors or files says why). A page
        the plain fetch is refused, or gets next to nothing from, is tried in the browser
        (see Reader) unless browse is False."""
        url = url.strip()
        if url in self.pages:
            return self.pages[url]
        if url in self.errors or url in self.files:
            return None
        text, reason, html = "", "", True
        try:
            text, html = page_text(url, True)
        except NotText as e:
            self.files[url] = str(e)
            return None
        except urllib.error.HTTPError as e:
            self.errors[url] = why(e)
            if e.code not in BROWSER_HTTP:
                return None
            reason = why(e)
        except (Refused, ValueError, OSError, UnicodeError) as e:
            self.errors[url] = why(e)
            return None
        if not reason and html and len(text) < THIN:
            reason = ("no readable text" if not text else f"only {len(text)} characters of text") \
                     + " (it may need JavaScript)"
        if reason and browse:
            seen = self._browse(url, reason)
            if seen and len(seen) > len(text):
                text = seen
                self.errors.pop(url, None)
            elif url in self.browsed:  # the browser saw no more than the plain fetch did
                del self.browsed[url]
        if not text:
            self.errors.setdefault(url, reason or "no readable text (it may need JavaScript)")
            return None
        self.errors.pop(url, None)
        self.pages[url] = text
        self.seen.update(norm(u) for u in urls_in(text))
        return text

    def exists(self, url: str) -> bool:
        """Whether something answers at the URL: a page, or a file that can't be read here."""
        self.open(url, browse=False)  # a probe isn't worth a minute of browser
        return url.strip() in self.pages or url.strip() in self.files

    def opened(self, url: str) -> bool:
        n = norm(url)
        return any(norm(u) == n for u in self.pages)
