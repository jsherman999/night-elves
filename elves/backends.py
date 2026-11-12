"""Model backends. Each one takes a prompt and returns a Result."""
from __future__ import annotations
import base64, json, os, signal, subprocess, urllib.request
from dataclasses import dataclass

class BackendError(RuntimeError):
    pass

@dataclass
class Result:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0

def _post(url: str, headers: dict, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:  # network, HTTP status, bad JSON
        detail = ""
        if hasattr(e, "read"):
            detail = e.read().decode(errors="replace")[:500]
        raise BackendError(f"{url}: {e} {detail}".strip()) from e

class OpenAICompat:
    """Any OpenAI-compatible server: Ollama, LM Studio, mlx_lm.server, llama.cpp."""
    def __init__(self, name, c):
        self.name, self.c = name, c
    def run(self, prompt, system=None, images=()) -> Result:
        if images:
            raise BackendError(f"{self.name}: this backend doesn't take images")
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        key = os.environ.get(self.c.get("api_key_env", ""), "none")
        d = _post(self.c["base_url"].rstrip("/") + "/chat/completions",
                  {"Authorization": f"Bearer {key}"},
                  {"model": self.c["model"], "messages": msgs,
                   "max_tokens": self.c.get("max_tokens", 4096)},
                  self.c.get("timeout", 1800))
        try:
            text = d["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise BackendError(f"{self.name}: unexpected response {str(d)[:300]}") from e
        u = d.get("usage") or {}
        return Result(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0.0)

class AnthropicAPI:
    """Claude API with a Console key; this is what spends the monthly API credit."""
    def __init__(self, name, c):
        self.name, self.c = name, c
    def run(self, prompt, system=None, images=()) -> Result:
        key = os.environ.get(self.c.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not key:
            raise BackendError(f"{self.name}: API key env var is not set")
        content = prompt
        if images:
            from .attachments import TYPES
            try:
                content = [{"type": "image", "source": {
                    "type": "base64", "media_type": TYPES[p.suffix.lstrip(".")],
                    "data": base64.b64encode(p.read_bytes()).decode()}} for p in images]
            except (OSError, KeyError) as e:
                raise BackendError(f"{self.name}: can't attach image: {e}") from e
            content.append({"type": "text", "text": prompt})
        body = {"model": self.c["model"], "max_tokens": self.c.get("max_tokens", 4096),
                "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        d = _post("https://api.anthropic.com/v1/messages",
                  {"x-api-key": key, "anthropic-version": "2023-06-01"},
                  body, self.c.get("timeout", 900))
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage") or {}
        tin, tout = u.get("input_tokens", 0), u.get("output_tokens", 0)
        usd = (tin * self.c.get("price_in", 0.0) + tout * self.c.get("price_out", 0.0)) / 1e6
        return Result(text, tin, tout, usd)

class Command:
    """A CLI that takes a prompt and prints an answer: `claude -p`, `codex exec`, `hermes`.
    Put "{prompt}" in argv to pass the prompt as an argument; otherwise it goes on stdin.
    Runs without a shell. Flat-rate tools record $0 and are limited by runs per night.
    Variables in env_unset are removed first: with ANTHROPIC_API_KEY set, `claude -p` bills the
    API instead of the subscription, and the ledger would record that spend as $0. ELVES_TOKEN
    goes too, so a tool-using agent can't queue tasks for itself. `env` adds variables.
    It runs in its own process group, so a timeout also kills anything it started."""
    def __init__(self, name, c):
        self.name, self.c = name, c
    def run(self, prompt, system=None, cwd=None, images=()) -> Result:
        """images must be inside cwd (Claude Code reads files there without asking)."""
        if images:
            listing = "\n".join(f"- {p}" for p in images)
            prompt += ("\n\nImages attached to this task (look at each one with the Read tool "
                       f"before you start):\n{listing}")
        full = f"{system}\n\n{prompt}" if system else prompt
        argv = [os.path.expanduser(a) if a.startswith("~/") else a for a in self.c["argv"]]
        as_arg = any("{prompt}" in a for a in argv)
        argv = [a.replace("{prompt}", full) for a in argv]
        drop = set(self.c.get("env_unset", ["ANTHROPIC_API_KEY", "ELVES_TOKEN"]))
        env = {k: v for k, v in os.environ.items() if k not in drop}
        for k, v in self.c.get("env", {}).items():  # "{cwd}" is the task's workspace
            if "{cwd}" not in v or cwd:
                env[k] = os.path.expanduser(v.replace("{cwd}", cwd or ""))
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL if as_arg else subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=env, cwd=cwd, start_new_session=True)
        except OSError as e:
            raise BackendError(f"{self.name}: {e}") from e
        try:
            out, err = p.communicate(None if as_arg else full, timeout=self.c.get("timeout", 1800))
        except subprocess.TimeoutExpired as e:
            for sig in (signal.SIGTERM, signal.SIGKILL):  # TERM first lets claude stop its tools
                try:
                    os.killpg(p.pid, sig)
                    p.communicate(timeout=10)
                    break
                except ProcessLookupError:
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise BackendError(f"{self.name}: {e}") from e
        if p.returncode != 0:
            raise BackendError(f"{self.name}: exit {p.returncode}: {err.strip()[:500]}")
        return Result(out.strip())

TYPES = {"openai_compat": OpenAICompat, "anthropic_api": AnthropicAPI, "command": Command}

def build(cfg: dict) -> dict:
    out = {}
    for name, c in cfg["backends"].items():
        if not c.get("enabled", True):
            continue
        if c.get("type") not in TYPES:
            raise ValueError(f"backend {name}: unknown type {c.get('type')!r}")
        out[name] = TYPES[c["type"]](name, c)
    return out
