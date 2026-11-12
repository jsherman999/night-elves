"""A small Markdown renderer for result pages: the subset models write (headings, paragraphs,
lists, task lists, fenced code, block quotes, tables, rules, links, emphasis, inline code).

Safety comes first: every piece of source text is HTML-escaped before any tag is added, raw
HTML in the source is shown as text, and links are kept only for http(s), mailto and
same-site paths, so a result can't run script in the page."""
from __future__ import annotations
import re
from html import escape

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
RULE = re.compile(r"^ {0,3}([-*_])(\s*\1){2,}\s*$")
ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])\s+(.*)$")
QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
SAFE_URL = re.compile(r"^(https?://|mailto:|/(?!/)|#)", re.I)

def render(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").expandtabs(4)
    return _blocks(lines.split("\n"))

def _starts_block(lines: list[str], i: int) -> bool:
    ln = lines[i]
    return bool(FENCE.match(ln) or HEADING.match(ln) or RULE.match(ln) or QUOTE.match(ln)
                or ITEM.match(ln) or _is_table(lines, i))

def _is_table(lines: list[str], i: int) -> bool:
    return ("|" in lines[i] and i + 1 < len(lines) and "-" in lines[i + 1]
            and bool(TABLE_SEP.match(lines[i + 1])))

def _blocks(lines: list[str]) -> str:
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
        elif m := FENCE.match(ln):
            fence, lang, body = m.group(1), m.group(2), []
            i += 1
            while i < n and not lines[i].lstrip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end)
            cls = f' class="lang-{escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{escape(chr(10).join(body))}</code></pre>")
        elif m := HEADING.match(ln):
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
        elif RULE.match(ln):
            out.append("<hr>")
            i += 1
        elif QUOTE.match(ln):
            body = []
            while i < n and lines[i].strip():
                q = QUOTE.match(lines[i])
                if not q and _starts_block(lines, i):
                    break
                body.append(q.group(1) if q else lines[i].strip())  # or a lazy continuation
                i += 1
            out.append(f"<blockquote>{_blocks(body)}</blockquote>")
        elif _is_table(lines, i):
            i = _table(lines, i, out)
        elif ITEM.match(ln):
            i = _list(lines, i, out)
        else:
            para = [ln.strip()]
            i += 1
            while i < n and lines[i].strip() and not _starts_block(lines, i):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{'<br>'.join(inline(p) for p in para)}</p>")
    return "\n".join(out)

def _list(lines: list[str], i: int, out: list[str]) -> int:
    first = ITEM.match(lines[i])
    indent, ordered = len(first.group(1)), first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items, n = [], len(lines)
    while i < n:
        m = ITEM.match(lines[i])
        if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
            break
        body, loose = [m.group(3)], False
        off = indent + len(m.group(2)) + 1  # where the item's text starts; deeper lines belong to it
        i += 1
        while i < n:
            ln = lines[i]
            if not ln.strip():  # a blank line continues the item only if indented text follows
                j = i + 1
                if j < n and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) > indent:
                    body.append("")
                    loose = loose or not ITEM.match(lines[j])
                    i += 1
                    continue
                break
            lead = len(ln) - len(ln.lstrip())
            if lead > indent:
                body.append(ln[min(lead, off):])
            elif ITEM.match(ln) or _starts_block(lines, i):
                break
            else:
                body.append(ln.strip())  # a lazy continuation line
            i += 1
        items.append((body, loose))
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    parts = []
    for body, loose in items:
        box = ""
        if mm := re.match(r"^\[([ xX])\]\s+", body[0]):
            box = "☑ " if mm.group(1) in "xX" else "☐ "
            body = [body[0][mm.end():], *body[1:]]
        html = _blocks(body)
        if not loose and html.startswith("<p>"):  # tight list: no paragraph around the text
            html = re.sub(r"^<p>(.*?)</p>", r"\1", html, count=1, flags=re.S)
        parts.append(f"<li>{box}{html}</li>")
    out.append(f"<{tag}{attr}>{''.join(parts)}</{tag}>")
    return i

def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]

def _table(lines: list[str], i: int, out: list[str]) -> int:
    head = _cells(lines[i])
    aligns = []
    for c in _cells(lines[i + 1]):
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left" if left else "")
    def row(cells, tag):
        cells = (cells + [""] * len(head))[:len(head)]
        return "<tr>" + "".join(
            f"<{tag}{f' style=text-align:{a}' if a else ''}>{inline(c)}</{tag}>"
            for c, a in zip(cells, aligns + [""] * len(head))) + "</tr>"
    rows = [row(head, "th")]
    i += 2
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(row(_cells(lines[i]), "td"))
        i += 1
    out.append(f"<div class=tbl><table><thead>{rows[0]}</thead><tbody>{''.join(rows[1:])}"
               "</tbody></table></div>")
    return i

def _link(label_html: str, url: str) -> str:
    if not SAFE_URL.match(url):
        return label_html
    return f'<a href="{escape(url)}" rel="noopener noreferrer">{label_html}</a>'

def inline(text: str) -> str:
    """One line of Markdown to HTML. Code spans and links are swapped for placeholders first
    so that emphasis can't reach inside them."""
    held: list[str] = []
    def hold(html: str) -> str:
        held.append(html)
        return f"\x00{len(held) - 1}\x00"
    text = re.sub(r"(`+)(.+?)\1", lambda m: hold(f"<code>{escape(m.group(2).strip())}</code>"),
                  text)
    text = re.sub(r"!?\[([^\]]+)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))+)>?(?:\s+\"[^\"]*\")?\s*\)",
                  lambda m: hold(_link(_emphasis(escape(m.group(1))), m.group(2))), text)
    text = re.sub(r"<(https?://[^>\s]+)>", lambda m: hold(_link(escape(m.group(1)), m.group(1))),
                  text)
    def bare(m):
        url = m.group(0)
        trail = re.search(r"[.,;:!?)\]'\"]+$", url)
        cut = trail.group(0) if trail and not (trail.group(0).startswith(")") and "(" in url) else ""
        url = url[:len(url) - len(cut)] if cut else url
        return hold(_link(escape(url), url)) + cut  # escaped with the rest of the line
    text = re.sub(r"https?://[^\s<>\x00]+", bare, text)
    text = _emphasis(escape(text))
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)

def _emphasis(s: str) -> str:
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<strong>\2</strong>", s)
    s = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", s)
    return re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", s)
