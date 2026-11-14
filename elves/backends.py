"""Model backends. Each one takes a prompt and returns a Result."""
from __future__ import annotations
import base64, json, os, signal, subprocess, urllib.request
from dataclasses import dataclass

class BackendError(RuntimeError):
    pass

class CeilingError(BackendError):
    """The task has used its share of API credit (budget.max_usd_per_task); trying again
    wouldn't help."""

class SpentError(BackendError):
    """A call that failed after it was billed; result holds the tokens and cost to record."""
    def __init__(self, msg, result):
        super().__init__(msg)
        self.result = result

@dataclass
class Result:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0          # billed against the API credit (anthropic_api only)
    est_usd: float | None = None  # what the work would cost at API prices; None = usd

SERVER_TOOLS = {"web_search": "web_search_20260209", "web_fetch": "web_fetch_20260209"}
SEARCH_USD = 0.01  # per web search request
MAX_CONTINUES = 8  # pause_turn continuations of one request (long server-tool loops)
WRAP_UP = ("Stop here: this task has reached its spending limit, so there is no more searching "
           "or reading. Write the finished result now from what you have gathered. Say plainly "
           "what you could not check or finish.")

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
    def run(self, prompt, system=None, files=()) -> Result:
        """files: images, sent as data URLs, if the backend has vision = true. The backend's
        `extra` table is added to the request as it is (reasoning_effort, temperature...)."""
        content = prompt
        if files:
            if not self.c.get("vision"):
                raise BackendError(f"{self.name}: this backend can't read images")
            from .attachments import TYPES
            content = [{"type": "text", "text": prompt}]
            for p in files:
                media = TYPES.get(p.suffix.lstrip(".").lower())
                try:
                    if not media:
                        raise KeyError(p.suffix)
                    data = base64.b64encode(p.read_bytes()).decode()
                except (OSError, KeyError) as e:
                    raise BackendError(f"{self.name}: can't attach {p.name}: {e}") from e
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{data}"}})
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": content}]
        key = os.environ.get(self.c.get("api_key_env", ""), "none")
        d = _post(self.c["base_url"].rstrip("/") + "/chat/completions",
                  {"Authorization": f"Bearer {key}"},
                  {"model": self.c["model"], "messages": msgs,
                   "max_tokens": self.c.get("max_tokens", 4096), **self.c.get("extra", {})},
                  self.c.get("timeout", 1800))
        try:
            text = (d["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.name}: unexpected response {str(d)[:300]}") from e
        u = d.get("usage") or {}
        return Result(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0.0)

class AnthropicAPI:
    """Claude API with a Console key; this is what spends the monthly API credit."""
    def __init__(self, name, c):
        self.name, self.c = name, c
    def run(self, prompt, system=None, files=(), budget_usd=None) -> Result:
        """files: images and PDFs (by suffix), sent ahead of the prompt. budget_usd: what this
        call may still cost. Anthropic's tool loop hands the turn back every ten steps
        (pause_turn); once the cost so far has reached the budget it isn't sent back to carry
        on, but once more without tools, to write the answer from what was gathered. So the
        budget can be passed by up to ten steps plus that last call."""
        key = os.environ.get(self.c.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not key:
            raise BackendError(f"{self.name}: API key env var is not set")
        content = prompt
        if files:
            from .attachments import TYPES
            content = []
            for p in files:
                ext = p.suffix.lstrip(".").lower()
                kind, media = (("document", "application/pdf") if ext == "pdf"
                               else ("image", TYPES.get(ext)))
                try:
                    if not media:
                        raise KeyError(ext)
                    data = base64.b64encode(p.read_bytes()).decode()
                except (OSError, KeyError) as e:
                    raise BackendError(f"{self.name}: can't attach {p.name}: {e}") from e
                content.append({"type": kind, "source": {"type": "base64", "media_type": media,
                                                         "data": data}})
            content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        # Thinking is always on for current Opus models and counts against max_tokens, so
        # leave plenty: 8k was used up by thinking before any answer on hard tasks.
        body = {"model": self.c["model"], "max_tokens": self.c.get("max_tokens", 32000),
                "messages": messages}
        if self.c.get("effort"):
            body["output_config"] = {"effort": self.c["effort"]}
        if system:
            body["system"] = system
        tools = [{"type": SERVER_TOOLS[t], "name": t, "max_uses": self.c.get("max_tool_uses", 20)}
                 for t in self.c.get("server_tools", []) if t in SERVER_TOOLS]
        if tools:
            body["tools"] = tools
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if self.c.get("fallbacks", True):  # a declined request is re-run on another model
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            body["fallbacks"] = "default"
        tin = tout = searches = 0
        def cost():
            return ((tin * self.c.get("price_in", 0.0) + tout * self.c.get("price_out", 0.0)) / 1e6
                    + searches * SEARCH_USD)
        capped = False
        for _ in range(MAX_CONTINUES + 1):
            try:
                d = _post("https://api.anthropic.com/v1/messages", headers, body,
                          self.c.get("timeout", 1800))
            except BackendError as e:
                if tin or tout:  # an earlier part of this turn was billed
                    raise SpentError(str(e), Result("", tin, tout, cost())) from e
                raise
            u = d.get("usage") or {}
            tin += (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0))
            tout += u.get("output_tokens", 0)
            searches += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
            if d.get("stop_reason") != "pause_turn" or capped:
                break
            # A long server-tool turn, handed back part-way: send it back to carry on, or, at
            # the spending limit, to finish without its tools.
            messages.append({"role": "assistant", "content": d.get("content", [])})
            if budget_usd is not None and cost() >= budget_usd:
                capped = True
                messages.append({"role": "user", "content": WRAP_UP})
                body.pop("tools", None)
        usd = cost()
        blocks = d.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if capped and text.strip():
            text = (text.rstrip() + f"\n\n---\n*night-elves: stopped at this task's spending "
                    f"limit (${usd:.2f} used in this call), so the answer above was written "
                    "from what had been gathered by then.*")
        if not text.strip():
            stop = d.get("stop_reason")
            if stop == "max_tokens":
                why = (f"used all {body['max_tokens']} output tokens (thinking counts) before "
                       "answering; raise max_tokens")
            elif stop == "refusal":
                det = d.get("stop_details") or {}
                why = f"declined ({det.get('category') or 'no category'}): {det.get('explanation') or ''}"
            else:
                why = f"no text in the reply (stop_reason {stop})"
            raise SpentError(f"{self.name}: {why}", Result("", tin, tout, usd))
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
    def run(self, prompt, system=None, cwd=None, args=()) -> Result:
        """args: extra arguments put after argv, such as a session id."""
        full = f"{system}\n\n{prompt}" if system else prompt
        argv = [os.path.expanduser(a) if a.startswith("~/") else a for a in self.c["argv"]]
        as_arg = any("{prompt}" in a for a in argv)
        argv = [a.replace("{prompt}", full) for a in argv] + list(args)
        env = environ(self.c, cwd)
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
        try:  # a server an agent forgot to stop would hold its port when the check runs
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        res = parse_output(self.c.get("output", "text"), out)
        if p.returncode != 0:  # `claude -p` prints some errors ("Reached max turns") on stdout
            detail = (err.strip() or res.text.strip() or out.strip())[-500:]
            raise SpentError(f"{self.name}: exit {p.returncode}: {detail}", res)
        return res

def parse_output(kind: str, out: str) -> Result:
    """A command's stdout as a Result. "claude-json" is `claude -p --output-format json`;
    "opencode-json" is `opencode run --format json` (one event per line). Their token counts
    and API-price estimates are recorded; a flat-rate tool bills nothing (usd 0)."""
    if kind == "claude-json":
        try:
            d = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return Result(out.strip())
        u = d.get("usage") or {}
        tin = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
               + u.get("cache_creation_input_tokens", 0))
        return Result(str(d.get("result") or "").strip(), tin, u.get("output_tokens", 0), 0.0,
                      float(d.get("total_cost_usd") or 0.0))
    if kind == "opencode-json":
        texts, tin, tout, cost, other = [], 0, 0, 0.0, []
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                if line.strip():
                    other.append(line)
                continue
            part = ev.get("part") or {}
            if ev.get("type") == "text" and part.get("text"):
                texts.append(part["text"].strip())
            elif ev.get("type") == "step_finish":
                t = part.get("tokens") or {}
                c = t.get("cache") or {}
                tin += t.get("input", 0) + c.get("read", 0) + c.get("write", 0)
                tout += t.get("output", 0) + t.get("reasoning", 0)
                cost += float(part.get("cost") or 0.0)
            elif ev.get("type") == "error":
                texts.append(f"Error: {json.dumps(ev.get('error') or ev)[:500]}")
        return Result("\n\n".join(texts + other).strip(), tin, tout, 0.0, cost)
    return Result(out.strip())

def environ(c: dict, cwd: str | None) -> dict:
    """The environment a command backend (and an agent's check) runs with: env_unset removed,
    then the backend's env table added, with "{cwd}" replaced by the task's workspace."""
    drop = set(c.get("env_unset", ["ANTHROPIC_API_KEY", "ELVES_TOKEN"]))
    env = {k: v for k, v in os.environ.items() if k not in drop}
    for k, v in c.get("env", {}).items():
        if "{cwd}" not in v or cwd:
            env[k] = os.path.expanduser(v.replace("{cwd}", cwd or ""))
    return env

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
