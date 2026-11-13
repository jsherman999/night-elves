"""Pushes for agent tasks, done outside the sandbox.

The sandbox has no GitHub credentials, and must not: a token there could be used for anything.
Instead an agent commits in the repository it cloned into its workspace and ends its reply with
`PUSH: <folder> <branch>`. Once its check has passed, night-elves pushes that branch from the
host, but only when the task itself asked for a push and the repository's origin is on GitHub
under an owner listed in [push].owners. Pushes are never forced.

Git is never run on the host inside the agent's repository, whose config and hooks the agent
controls (core.fsmonitor and the like would run on the host). The branch is bundled inside the
check sandbox, and the host fetches that bundle, which is plain data, into a fresh repository of
its own and pushes from there."""
from __future__ import annotations
import re, shlex, shutil, subprocess, tempfile
from pathlib import Path
from . import verify

PUSH_RE = re.compile(r"^[\s>*_#`-]*PUSH[*_]*\s*:[*_`]*\s*(\S+)\s+(\S+?)[`*_]*\s*$", re.I | re.M)
ASKS = re.compile(r"\bpush(?:es|ed|ing)?\b", re.I)
GITHUB = re.compile(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
NOTE = ("This task asks for a push. The sandbox has no GitHub credentials, so don't try to push "
        "or look for tokens. Clone the repository into the workspace, commit your changes in that "
        "clone on the branch the task wants (the repository's default branch if it doesn't say), "
        "and put a line `PUSH: <clone folder> <branch>` just before the STATUS line. Once your "
        "check passes, night-elves pushes that branch to the clone's GitHub origin from outside "
        "the sandbox. The push is never forced, so build on the remote branch, not beside it.")
GIT_TIMEOUT = 600
DEST = "https://github.com/{owner}/{name}.git"  # where a push goes (tests point it elsewhere)

def asked(prompt: str) -> bool:
    return bool(ASKS.search(prompt))

def requests(answer: str) -> list[tuple[str, str]]:
    tail = "\n".join(answer.strip().splitlines()[-40:])
    return [(d.strip("`"), b.strip("`")) for d, b in PUSH_RE.findall(tail)]

def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    # The host's own config (for the gh credential helper) but nothing that runs code.
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                           "-c", "transfer.fsckObjects=true", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=GIT_TIMEOUT,
                          stdin=subprocess.DEVNULL)

def _one(cfg, work: Path, folder: str, branch: str, env: dict) -> str:
    owners = {o.lower() for o in cfg["push"]["owners"]}
    root = work.resolve()
    repo = (work / folder).resolve()
    if repo != root and root not in repo.parents:
        return f"`{folder}` is outside the workspace; not pushed."
    if not (repo / ".git").is_dir():
        return f"`{folder}` is not a git clone (no .git folder); not pushed."
    if _git("check-ref-format", "--branch", branch).returncode or branch.startswith("-"):
        return f"`{branch}` is not a valid branch name; not pushed."
    url = subprocess.run(["git", "config", "--file", str(repo / ".git" / "config"), "--get",
                          "remote.origin.url"], capture_output=True, text=True).stdout.strip()
    m = GITHUB.match(url)
    if not m:
        return f"`{folder}`'s origin ({url or 'none'}) is not a GitHub repository; not pushed."
    owner, name = m.groups()
    if owner.lower() not in owners:
        return (f"{owner}/{name} is not under an owner in [push].owners "
                f"({', '.join(sorted(owners)) or 'none'}); not pushed.")
    dest = DEST.format(owner=owner, name=name)
    bundle = work / ".push" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', branch)}.bundle"
    bundle.parent.mkdir(exist_ok=True)
    bundle.unlink(missing_ok=True)
    cmd = (f"git -C {shlex.quote(str(repo))} bundle create {shlex.quote(str(bundle))} "
           f"{shlex.quote('refs/heads/' + branch)}")
    code, out = verify.run_check(cmd, work, env, GIT_TIMEOUT, verify.secrets(cfg))
    if code != 0 or not bundle.is_file():
        return f"Couldn't bundle `{branch}` in `{folder}` (exit {code}):\n```\n{out}\n```"
    tmp = Path(tempfile.mkdtemp(prefix="elves-push-"))
    try:
        _git("init", "-q", "--bare", str(tmp))
        ref = f"refs/heads/{branch}:refs/heads/{branch}"
        got = _git("-C", str(tmp), "fetch", "-q", str(bundle), ref)
        if got.returncode:  # a shallow clone's bundle needs the history it was cut from
            _git("-C", str(tmp), "fetch", "-q", dest, "+refs/heads/*:refs/remotes/origin/*")
            got = _git("-C", str(tmp), "fetch", "-q", str(bundle), ref)
        if got.returncode:
            return f"Couldn't read the bundle of `{branch}`:\n```\n{got.stderr.strip()[-1500:]}\n```"
        sent = _git("-C", str(tmp), "push", "--porcelain", dest, ref)
        tail = (sent.stdout + sent.stderr).strip()[-1500:]
        if sent.returncode:
            return f"Push of `{branch}` to {owner}/{name} failed:\n```\n{tail}\n```"
        return f"Pushed `{branch}` to {owner}/{name}.\n```\n{tail}\n```"
    except (OSError, subprocess.SubprocessError) as e:
        return f"Push of `{branch}` to {owner}/{name} failed: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def run(cfg, work: Path, prompt: str, answer: str, env: dict) -> str:
    """Push what the agent asked for. Returns a Markdown note for the result ("" if none)."""
    reqs = requests(answer)
    if not reqs:
        return ("The task asked for a push, but the agent's reply had no PUSH line, so nothing "
                "was pushed." if asked(prompt) else "")
    if not asked(prompt):
        return "The agent asked for a push, but the task didn't, so nothing was pushed."
    if not cfg["push"]["owners"]:
        return "Pushes are off ([push].owners is empty in config.toml), so nothing was pushed."
    return "\n\n".join(_one(cfg, work, f, b, env) for f, b in reqs[:5])
