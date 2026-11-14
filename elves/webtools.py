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
import gzip, ipaddress, json, re, socket, urllib.error, urllib.parse, urllib.request, zlib
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

def page_text(url: str) -> str:
    """The text of a public page. Raises Refused or OSError with a reason fit for the model."""
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
    return html_text(text, final) if "html" in ctype else text.strip()

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
    def __init__(self, search_url: str):
        self.search_url = search_url
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

    def open(self, url: str) -> str | None:
        """The page's text, or None if it couldn't be read (errors or files says why)."""
        url = url.strip()
        if url in self.pages:
            return self.pages[url]
        if url in self.errors or url in self.files:
            return None
        try:
            text = page_text(url)
        except NotText as e:
            self.files[url] = str(e)
            return None
        except (Refused, ValueError, OSError, UnicodeError) as e:
            self.errors[url] = why(e)
            return None
        if not text:
            self.errors[url] = "no readable text (it may need JavaScript)"
            return None
        self.pages[url] = text
        self.seen.update(norm(u) for u in urls_in(text))
        return text

    def exists(self, url: str) -> bool:
        """Whether something answers at the URL: a page, or a file that can't be read here."""
        self.open(url)
        return url.strip() in self.pages or url.strip() in self.files

    def opened(self, url: str) -> bool:
        n = norm(url)
        return any(norm(u) == n for u in self.pages)
