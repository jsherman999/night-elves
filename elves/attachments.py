"""Files attached to a task, stored in attachments/<task id>/.

Each file gets a kind when it arrives, which decides who can work on it:
  image  PNG/JPEG/GIF/WebP/HEIC (HEIC converted, big ones shrunk): Claude, or a local
         vision model when one is configured (roles.local_vision)
  pdf    up to 30 MB: Claude (the API reads it; claude -p opens it), or the local vision
         model for PDFs of up to LOCAL_PDF_PAGES pages, sent as page images
  text   UTF-8, up to INLINE_MAX for all of a task's text files together: pasted into the
         prompt, so any model can use it
  data   anything else, or text too big to paste: the agent tier only, which can run code on it

Names on disk are generated here: the position, then the upload's own name with anything
outside [A-Za-z0-9._-] replaced, e.g. 2-sales_2026.csv. Files are found by their index in the
task's `files` list, so no path from a request is ever opened."""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, tempfile
from pathlib import Path

MAX_FILES = 20
MAX_EDGE = 2048                 # longer side of an image after normalising
API_MAX_BYTES = 3_750_000       # keeps an image's base64 form under the API's 5 MB
PDF_MAX = 30 * 1024 * 1024      # the API takes up to 32 MB per request
INLINE_MAX = 200_000            # bytes of text pasted into a prompt, all files together
LOCAL_PDF_PAGES = 20            # longest PDF the local vision model gets, as page images
PAGE_EDGE = 1600                # pixels, longer side of a rendered PDF page
PDF_TOOL = Path(__file__).with_name("pdfpages.swift")
TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
VISUAL = {"image", "pdf"}  # kinds a model has to see, not read as text

def sniff(head: bytes) -> str | None:
    """The image type from a file's first bytes, or None. HEIC/HEIF (iPhone photos) is 'heic'."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"heim", b"heis", b"hevc",
                                                b"hevx", b"mif1", b"msf1"):
        return "heic"
    return None

def files(task) -> list[dict]:
    raw = task["files"] if "files" in task.keys() else None
    return json.loads(raw) if raw else []

def kinds(task) -> set[str]:
    return {f["kind"] for f in files(task)}

def task_dir(cfg: dict, tid: int) -> Path:
    return Path(cfg["paths"]["attachments"]) / str(tid)

def paths(cfg: dict, task, only=None) -> list[Path]:
    return [task_dir(cfg, task["id"]) / f["name"] for f in files(task)
            if only is None or f["kind"] in only]

def local_vision(cfg: dict) -> bool:
    """Whether a local model that can see images is configured."""
    name = cfg["roles"].get("local_vision")
    c = cfg["backends"].get(name) if name else None
    return bool(c and c.get("enabled", True) and c.get("vision"))

def local_can_see(fs: list[dict]) -> bool:
    """Whether a local vision model can take all of these files' images and PDFs."""
    return all(f["kind"] != "pdf" or (f.get("pages") or 0) and f["pages"] <= LOCAL_PDF_PAGES
               for f in fs)

def check_tier(tier: str | None, fs: list[dict], vision: bool = False) -> None:
    """Raise ValueError if this tier can't work with these files. vision: a local vision
    model is configured (local_vision(cfg))."""
    ks = {f["kind"] for f in fs}
    if tier is None or not ks:
        return
    if "data" in ks and tier != "agent":
        raise ValueError("big or non-text files need the agent tier, which can run code on "
                         "them (text files up to 200 KB in all work with any tier)")
    if ks & VISUAL and tier == "local":
        if not vision:
            raise ValueError("the local model can't see images or PDFs; use auto, frontier "
                             "or agent")
        if not local_can_see(fs):
            raise ValueError(f"the local model takes PDFs of up to {LOCAL_PDF_PAGES} pages; "
                             "use auto, frontier or agent")

def size_label(n: int | None) -> str:
    if n is None:
        return "size unknown"
    for unit in ("bytes", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"

def _safe(name: str | None) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").name).strip("._-")
    return base[-80:] or "file"

def _sips(*args) -> str:
    if not shutil.which("sips"):
        raise ValueError("converting this image needs macOS's sips tool")
    try:
        return subprocess.run(["sips", *args], capture_output=True, text=True, timeout=120,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"couldn't convert the image: {e}") from None

def _edge(p: Path) -> int:
    out = _sips("-g", "pixelWidth", "-g", "pixelHeight", str(p))
    return max((int(n) for n in re.findall(r"pixel(?:Width|Height):\s*(\d+)", out)), default=0)

def _normalise(p: Path, kind: str) -> Path:
    """HEIC becomes JPEG; anything larger than MAX_EDGE or API_MAX_BYTES is shrunk. Without
    sips (not macOS), PNG/JPEG/GIF/WebP are kept as they are."""
    if kind == "gif" or (kind != "heic" and not shutil.which("sips")):
        return p
    wide = _edge(p) > MAX_EDGE
    if kind != "heic" and not wide and p.stat().st_size <= API_MAX_BYTES:
        return p
    out = p.with_suffix(".jpg") if kind in ("heic", "webp") else p
    _sips(*(["-Z", str(MAX_EDGE)] if wide else []),  # -Z also enlarges, so only when needed
          *(["-s", "format", "jpeg"] if out != p else []), str(p), "--out", str(out))
    if out.stat().st_size > API_MAX_BYTES:  # a huge PNG even after resizing
        out2 = p.with_suffix(".jpg")
        _sips("-s", "format", "jpeg", "-s", "formatOptions", "85", str(out), "--out", str(out2))
        out = out2
    if out != p:
        p.unlink(missing_ok=True)
    return out

def _pdf_tool(root: Path) -> Path | None:
    """The PDF helper, compiled from pdfpages.swift on first use into attachments/.tools/.
    None where it can't be built (not macOS, no Xcode command line tools)."""
    src = PDF_TOOL.read_bytes()
    exe = root / ".tools" / f"pdfpages-{hashlib.sha256(src).hexdigest()[:12]}"
    if exe.is_file():
        return exe
    if not shutil.which("swiftc"):
        return None
    exe.parent.mkdir(parents=True, exist_ok=True)
    tmp = exe.with_name(exe.name + f".{os.getpid()}")
    try:
        subprocess.run(["swiftc", "-O", str(PDF_TOOL), "-o", str(tmp)], check=True,
                       capture_output=True, timeout=300)
        tmp.rename(exe)
        return exe
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return None

def pdf_page_count(root: Path, pdf: Path) -> int | None:
    tool = _pdf_tool(root)
    if not tool:
        return None
    try:
        out = subprocess.run([str(tool), str(pdf)], capture_output=True, text=True,
                             timeout=60, check=True).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None

def pdf_pages(cfg: dict, task, f: dict) -> list[Path]:
    """A PDF's pages as PNGs (cached in attachments/<id>/.pages/), for a local vision model."""
    tdir = task_dir(cfg, task["id"])
    out = tdir / ".pages" / f["name"]
    want = min(f.get("pages") or LOCAL_PDF_PAGES, LOCAL_PDF_PAGES)
    pages = sorted(out.glob("page-*.png"))
    if len(pages) < want:
        tool = _pdf_tool(Path(cfg["paths"]["attachments"]))
        if not tool:
            raise ValueError("can't render PDF pages here (needs macOS and swiftc)")
        try:
            subprocess.run([str(tool), str(tdir / f["name"]), str(out), str(want),
                            str(PAGE_EDGE)], capture_output=True, timeout=600, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise ValueError(f"couldn't render {f['name']}: {e}") from None
        pages = sorted(out.glob("page-*.png"))
    return pages[:want]

def _is_text(p: Path) -> bool:
    try:
        data = p.read_bytes()  # only called for files under INLINE_MAX
        data.decode("utf-8")
        return b"\x00" not in data
    except (OSError, UnicodeDecodeError):
        return False

class Staged:
    """Files gathered in a temporary folder under attachments/, then moved under the task's id
    by commit(), which runs while the task row is inserted but not yet committed, so a run
    never sees a task without its files. Use as a context manager: leftovers are removed."""
    def __init__(self, cfg: dict):
        self.root = Path(cfg["paths"]["attachments"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix=".new-", dir=self.root))
        self.incoming: list[tuple[Path, str | None]] = []
        self.files: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.discard()

    def new_file(self, orig_name: str | None) -> Path:
        """A path to write one upload to; finish() sorts it out."""
        if len(self.incoming) >= MAX_FILES * 2:  # empty file fields from a form count too
            raise ValueError(f"at most {MAX_FILES} files per task")
        p = self.dir / f".in-{len(self.incoming)}"
        self.incoming.append((p, orig_name))
        return p

    def add_path(self, src: str | os.PathLike) -> None:
        shutil.copyfile(src, self.new_file(Path(src).name))

    def finish(self) -> list[dict]:
        """Classify, rename and normalise what arrived. Empty files are dropped."""
        got = [(p, n) for p, n in self.incoming if p.is_file() and p.stat().st_size]
        if len(got) > MAX_FILES:
            raise ValueError(f"at most {MAX_FILES} files per task")
        text_left = INLINE_MAX
        for i, (p, orig) in enumerate(got, 1):
            with open(p, "rb") as f:
                head = f.read(16)
            size, name = p.stat().st_size, f"{i}-{_safe(orig)}"
            if img := sniff(head):
                stem = name.rsplit(".", 1)[0] if "." in name else name
                dest = self.dir / f"{stem}.{img}"
                p.rename(dest)
                dest, kind = _normalise(dest, img), "image"
            else:
                pages = None
                if head.startswith(b"%PDF-") and size <= PDF_MAX:
                    kind, pages = "pdf", pdf_page_count(self.root, p)
                elif size <= text_left and _is_text(p):
                    kind, text_left = "text", text_left - size
                else:
                    kind = "data"
                dest = self.dir / name
                p.rename(dest)
            rec = {"name": dest.name, "kind": kind, "size": dest.stat().st_size}
            if kind == "pdf":
                rec["pages"] = pages
            self.files.append(rec)
        for p, _ in self.incoming:
            p.unlink(missing_ok=True)
        return self.files

    def commit(self, tid: int) -> None:
        dest = self.root / str(tid)
        if dest.exists():  # left over from a queue that was reset; this id is new
            shutil.rmtree(dest)
        self.dir.rename(dest)

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)  # nothing left to remove once committed

def add_task(cfg: dict, conn, prompt: str, staged: Staged, **kw) -> int:
    """db.add_task with the staged files: the row and the folder appear together or not at all."""
    from . import db
    return db.add_task(conn, prompt, files=staged.files, vision=local_vision(cfg),
                       on_insert=staged.commit if staged.files else None, **kw)

def inline_text(cfg: dict, task) -> str:
    """The task's text files, to paste in after the task."""
    out = []
    for f, p in zip(files(task), paths(cfg, task)):
        if f["kind"] == "text":
            body = p.read_text(errors="replace")
            fence = "````" if "```" in body else "```"
            out.append(f"\n\nAttached file {f['name']}:\n{fence}\n{body.rstrip()}\n{fence}")
    return "".join(out)

def listing(task, prefix: str = "") -> str:
    return "".join(f"\n- {prefix}{f['name']} ({f['kind']}, {size_label(f.get('size'))})"
                   for f in files(task))

def copy_into(cfg: dict, task, dest: Path) -> None:
    """Put copies of the task's files in dest (an agent's workspace). On APFS `cp -c` makes
    clones, which take no extra space until one side changes, so a big upload isn't stored
    twice and the agent can't change the original."""
    dest.mkdir(parents=True, exist_ok=True)
    for src in paths(cfg, task):
        target = dest / src.name
        if target.exists():
            continue
        try:
            subprocess.run(["cp", "-c", str(src), str(target)], check=True, capture_output=True,
                           timeout=600)
        except (OSError, subprocess.SubprocessError):
            shutil.copy2(src, target)
