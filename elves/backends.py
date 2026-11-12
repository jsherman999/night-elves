"""Model backends. Each one takes a prompt and returns a Result."""
from __future__ import annotations
import json, os, subprocess, urllib.request
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
    def run(self, prompt, system=None) -> Result:
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
    def run(self, prompt, system=None) -> Result:
        key = os.environ.get(self.c.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not key:
            raise BackendError(f"{self.name}: API key env var is not set")
        body = {"model": self.c["model"], "max_tokens": self.c.get("max_tokens", 4096),
                "messages": [{"role": "user", "content": prompt}]}
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
    API instead of the subscription, and the ledger would record that spend as $0."""
    def __init__(self, name, c):
        self.name, self.c = name, c
    def run(self, prompt, system=None) -> Result:
        full = f"{system}\n\n{prompt}" if system else prompt
        argv = list(self.c["argv"])
        as_arg = any("{prompt}" in a for a in argv)
        argv = [a.replace("{prompt}", full) for a in argv]
        drop = set(self.c.get("env_unset", ["ANTHROPIC_API_KEY"]))
        env = {k: v for k, v in os.environ.items() if k not in drop}
        try:
            p = subprocess.run(argv, input=None if as_arg else full, capture_output=True,
                               text=True, timeout=self.c.get("timeout", 1800), env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"{self.name}: {e}") from e
        if p.returncode != 0:
            raise BackendError(f"{self.name}: exit {p.returncode}: {p.stderr.strip()[:500]}")
        return Result(p.stdout.strip())

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
