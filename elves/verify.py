"""The agent tier's completion contract, and the sandbox its check runs in.

An agent ends its reply with `STATUS: DONE` (or `INCOMPLETE`) and `CHECK: <command>`, a command
that exits 0 only when the work is right. night-elves runs that command itself after the agent
has exited, under sandbox-exec: writes only in the workspace and the temp folders, no network
except localhost, no secrets, and no sudo, osascript, security, launchctl or ssh."""
from __future__ import annotations
import os, re, signal, subprocess, tempfile
from pathlib import Path

SANDBOX = "/usr/bin/sandbox-exec"
STATUS_RE = re.compile(r"^[\W_]*STATUS[\W_]*:[\W_]*(DONE|INCOMPLETE)\b", re.I | re.M)
CHECK_RE = re.compile(r"^[\s>*_#-]*CHECK[*_]*\s*:[*_]*\s*(.*?)\s*$", re.I | re.M)
NO_CHECK = {"", "none", "n/a", "na", "-"}
DENY_EXEC = ("/usr/bin/sudo", "/usr/bin/su", "/usr/bin/login", "/usr/bin/osascript",
             "/usr/bin/security", "/bin/launchctl", "/usr/bin/ssh", "/usr/bin/scp", "/usr/bin/sftp")
HOME_SECRETS = (".ssh", ".gnupg", ".aws", ".config", ".night-elves/env", "Library/Keychains",
                "Library/Messages")
TAIL = 3000  # characters of the check's output kept for the result and the feedback

def contract(answer: str) -> tuple[str | None, str | None]:
    """(status, check) from the end of an agent's reply. status is DONE, INCOMPLETE or None
    (missing); check is the command, "" for `CHECK: none`, or None (missing)."""
    tail = "\n".join(answer.strip().splitlines()[-40:])
    statuses, checks = STATUS_RE.findall(tail), CHECK_RE.findall(tail)
    status = statuses[-1].upper() if statuses else None
    if not checks:
        return status, None
    c = checks[-1].strip()
    if len(c) >= 2 and c[0] == c[-1] == "`":
        c = c.strip("`").strip()
    return status, "" if c.lower().rstrip(".") in NO_CHECK else c

def _q(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

def profile(work, deny_read=()) -> str:
    """A Seatbelt profile: everything the default allows, minus writes outside the workspace
    and temp, network beyond localhost, the listed reads, and the privileged commands."""
    real = [os.path.realpath(p) for p in deny_read]
    lines = ["(version 1)", "(allow default)",
             "(deny network-outbound)", '(allow network-outbound (remote ip "localhost:*"))',
             "(deny file-write*)",
             f"(allow file-write* (subpath {_q(os.path.realpath(work))}) (subpath \"/private/tmp\")"
             " (subpath \"/private/var/folders\") (subpath \"/dev\"))",
             "(deny process-exec " + " ".join(f"(literal {_q(p)})" for p in DENY_EXEC) + ")",
             '(deny mach-lookup (global-name "com.apple.coreservices.appleevents"))']
    if real:
        lines.append("(deny file-read* " + " ".join(f"(subpath {_q(p)})" for p in real) + ")")
    return "\n".join(lines) + "\n"

def secrets(cfg: dict) -> list[str]:
    """Paths a check may not read: the usual secrets and night-elves' own queue."""
    home = Path.home()
    db = cfg["paths"]["db"]
    return [str(home / p) for p in HOME_SECRETS] + [db, db + "-wal", db + "-shm", db + ".lock"]

def run_check(cmd: str, work, env: dict, timeout: int, deny_read=()) -> tuple[int | None, str]:
    """Run the agent's check in the sandbox. Returns (exit code, or None if it timed out, and
    the end of its output). Anything it leaves running is killed with its process group.
    Raises OSError if sandbox-exec is missing."""
    if not os.path.exists(SANDBOX):
        raise FileNotFoundError(f"{SANDBOX} not found")
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen([SANDBOX, "-p", profile(work, deny_read), "/bin/sh", "-c", cmd],
                             cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        for sig in (signal.SIGTERM, signal.SIGKILL):  # servers the check started, and so on
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        size = out.seek(0, os.SEEK_END)
        out.seek(max(size - TAIL, 0))
        text = out.read().decode(errors="replace")
    return code, ("…" if size > TAIL else "") + text.strip()
