"""Images attached to a task: checked, normalised and stored in attachments/<task id>/.

File names are always generated here (1.png, 2.jpg, ...), never taken from the upload, and a
task's images are looked up by their index in its `images` list, so no path from a request is
ever opened."""
from __future__ import annotations
import json, re, shutil, subprocess, tempfile
from pathlib import Path

MAX_IMAGES = 4
MAX_BYTES = 20 * 1024 * 1024    # one upload, before conversion
MAX_EDGE = 2048                 # longer side after normalising; Claude downsizes past ~1568 anyway
API_MAX_BYTES = 3_750_000       # keeps the base64 form under the API's 5 MB per image
TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}

def sniff(data: bytes) -> str | None:
    """The image type from its first bytes, or None. HEIC/HEIF (iPhone photos) is 'heic'."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"heim", b"heis", b"hevc",
                                                b"hevx", b"mif1", b"msf1"):
        return "heic"
    return None

def names(task) -> list[str]:
    raw = task["images"] if "images" in task.keys() else None
    return json.loads(raw) if raw else []

def task_dir(cfg: dict, tid: int) -> Path:
    return Path(cfg["paths"]["attachments"]) / str(tid)

def paths(cfg: dict, task) -> list[Path]:
    return [task_dir(cfg, task["id"]) / n for n in names(task)]

def _sips(*args) -> str:
    if not shutil.which("sips"):
        raise ValueError("converting this image needs macOS's sips tool")
    try:
        return subprocess.run(["sips", *args], capture_output=True, text=True, timeout=60,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"couldn't convert the image: {e}") from None

def _edge(p: Path) -> int:
    out = _sips("-g", "pixelWidth", "-g", "pixelHeight", str(p))
    return max((int(n) for n in re.findall(r"pixel(?:Width|Height):\s*(\d+)", out)), default=0)

def _normalise(p: Path, kind: str) -> Path:
    """HEIC becomes JPEG; anything larger than MAX_EDGE or API_MAX_BYTES is shrunk. Without
    sips (not macOS), PNG/JPEG/GIF/WebP are kept as they are."""
    if kind == "gif":
        return p
    if kind != "heic" and not shutil.which("sips"):
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

class Staged:
    """Uploads checked and normalised in a temporary folder, moved under the task's id by
    commit(). Use it while the task row is inserted but not yet committed, so a nightly run
    never sees a task whose images aren't in place."""
    def __init__(self, cfg: dict, blobs: list[bytes]):
        blobs = [b for b in blobs if b]
        if len(blobs) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} images per task")
        root = Path(cfg["paths"]["attachments"])
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.names = root, []
        self.dir = Path(tempfile.mkdtemp(prefix=".new-", dir=root))
        try:
            for i, data in enumerate(blobs, 1):
                if len(data) > MAX_BYTES:
                    raise ValueError(f"image {i} is over {MAX_BYTES // 2**20} MB")
                kind = sniff(data)
                if kind is None:
                    raise ValueError(f"file {i} isn't a PNG, JPEG, GIF, WebP or HEIC image")
                p = self.dir / f"{i}.{kind}"
                p.write_bytes(data)
                self.names.append(_normalise(p, kind).name)
        except BaseException:
            self.discard()
            raise

    def commit(self, tid: int) -> None:
        dest = self.root / str(tid)
        if dest.exists():  # left over from a queue that was reset; this id is new
            shutil.rmtree(dest)
        self.dir.rename(dest)

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

def add_task(cfg: dict, conn, prompt: str, blobs: list[bytes], **kw) -> int:
    """db.add_task with images: the row and the folder appear together, or not at all."""
    from . import db
    staged = Staged(cfg, blobs)
    try:
        return db.add_task(conn, prompt, images=staged.names,
                           on_insert=staged.commit if staged.names else None, **kw)
    finally:
        staged.discard()  # a no-op once committed

def copy_into(cfg: dict, task, dest: Path) -> list[Path]:
    """Copy a task's images into dest (an agent's workspace) and return the copies."""
    out = []
    for src in paths(cfg, task):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / src.name)
        out.append(dest / src.name)
    return out
