# night-elves

Drop tasks into a queue during the day from the CLI, a web form, or a text
message. Overnight, local models on the Mac Studio work through them. As the
billing cycle runs out, leftover Claude API credit is spent on planning,
review, and the hard tasks instead of expiring unused.

Standard library only. Python 3.11 or newer.

## How it works

```
 CLI ──┐                          ┌─ local      (Ollama / LM Studio / MLX)
 web ──┼─► SQLite queue ─► nightly ┼─ claude_api (monthly API credit, metered)
 SMS ──┘                  run     ├─ claude_cli (`claude -p`, subscription limits)
                                  └─ codex      (`codex exec`, optional reviewer)
```

Every model call is written to a `spend` ledger. The API credit is paced from
that ledger, so the dispatcher always knows how much is left and how many days
remain.

| Mode | When | Planner | Executor | Reviewer |
|---|---|---|---|---|
| conserve | first half of the cycle | none | local | codex, if enabled |
| pace | second half | frontier | local | frontier |
| endgame | last 7 days | frontier | frontier for priority 1-3, else local | frontier |

In every mode:

- A task with `--tier frontier` goes straight to the frontier model. If there is
  no allowance left tonight it stays queued.
- A task with `--tier agent` goes to Claude Code with tools, in a sandbox (see
  [Agent tier](#agent-tier)). Nothing else is ever routed there.
- A task that fails locally twice is escalated to the frontier model.
- "Frontier" means the API credit first. When tonight's allowance is used up,
  it falls back to `claude -p`, which only runs inside its time window and up
  to its nightly run cap, so the subscription is fresh in the morning.
- Tonight's allowance is `remaining / days_left * burst`, capped per night.
- Three failed attempts mark a task `failed`. A reviewer rejection is retried
  the same night with the feedback attached. A backend error costs the task one
  attempt and holds it until the next run, so an outage can't fail a task
  outright. A backend that errors three times in a row is skipped for the rest
  of the run.

## Install

```sh
brew install python            # macOS ships an older Python
git clone https://github.com/jsherman999/night-elves && cd night-elves
brew install pipx && pipx ensurepath
pipx install --editable .      # elf goes in ~/.local/bin; Homebrew Python refuses a plain pip install
mkdir -p ~/.night-elves && cp config.example.toml ~/.night-elves/config.toml
```

Edit `~/.night-elves/config.toml`:

1. Point `[backends.local]` at your model server and name the model.
2. Set `budget.cycle_day` to the day your Claude plan renews.
3. Set `price_in` and `price_out` for the Claude model from the current
   pricing page. The values in the example file are placeholders.
4. Put secrets in `~/.night-elves/env` and `chmod 600` it:

```sh
export ANTHROPIC_API_KEY=...   # from the Console org linked to your Max plan
export ELVES_TOKEN=...         # 16+ characters, for the web and SMS intake
```

## Use

```sh
elf add "Summarise the open issues in ~/notes/issues.md"
elf add -p 1 -t frontier "Design the schema for the access-review tool"
elf add -t agent "Capture 5 seconds of the Bluesky firehose to a JSON file"
elf add --at 01:30 -t agent "Record which hosts answer ping on the LAN"
cat spec.md | elf add -
elf list
elf show 3
elf budget              # mode, credit spent, tonight's allowance
elf run --dry-run       # show how each queued task would be routed
elf run                 # work the queue now
elf retry 3             # requeue a failed, cancelled or finished task
elf cancel 4 5          # take queued tasks out of the queue
elf edit 4 -t agent -p 2 --at 01:30    # also --prompt "...", --now, --tonight
```

`elf edit` and `elf cancel` only touch tasks that haven't started (queued, failed
or cancelled); once a run picks a task up, it is left alone. `--now` means
"within a minute" (the every-minute job below runs it, and sends the digest);
`--tonight` drops a scheduled time so the 01:00 run takes it. Backend windows
still apply, so an agent task set to run now during the day waits for 01:00.

Results land in `~/.night-elves/results/<id>.md`, with a `digest-<date>.txt`
after each run.

### Nightly schedule

```sh
sed "s/YOU/$USER/g" launchd/com.nightelves.nightly.plist > ~/Library/LaunchAgents/com.nightelves.nightly.plist
launchctl load ~/Library/LaunchAgents/com.nightelves.nightly.plist
```

It runs at 01:00 through `scripts/nightly.sh`, which loads the env file and
wraps the run in `caffeinate` so the Mac stays awake. Adjust the clone path in
the plist if the repo is not in `~/night-elves`.

`caffeinate` only holds the Mac awake once the run has started. If the Mac
sleeps, launchd runs the job when it next wakes, so either stop it sleeping on
power (`sudo pmset -c sleep 0`) or wake it just before the run
(`sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00`).

### Scheduled tasks

`--at` holds a task until a local time: `01:30`, `1:30am`, `2pm`, or
`2026-11-20 07:05`. A bare time means its next occurrence. From the phone,
start the prompt with `at 1:30am` (a colon or am/pm is needed, so "At 5
companies…" is left alone), or send an `at` field to `/tasks`.

A second launch agent, `launchd/com.nightelves.due.plist`, runs
`elf run --due-only` every minute. It only picks up scheduled tasks whose time
has come, and it is silent and sends no digest when there are none. If the
nightly run is busy, it leaves the task to that run, which takes due tasks
before anything else. Install it like the nightly plist.

What "at" means:

- It's "not before", accurate to about a minute when nothing else is running.
- The backend's window still applies: an agent or `claude -p` task set for
  2pm waits for the next 01:00-05:00 window. Local-model tasks run any time.
- A scheduled task that hits a backend error is retried 30 minutes later.
- Nightly caps (API allowance, runs per night) count from local midnight, so
  daytime scheduled runs share the same day's allowance with the night run.

### Web intake

```sh
elf serve               # http://127.0.0.1:8377
curl -H "Authorization: Bearer $ELVES_TOKEN" -H 'Content-Type: application/json' \
     -d '{"prompt":"...","priority":3,"tier":"auto"}' http://127.0.0.1:8377/tasks
```

The server binds to loopback; reach it from elsewhere through Tailscale or a
tunnel instead of opening the port. `launchd/com.nightelves.web.plist` keeps
it running.

**In a browser** (bookmark it, or Share > Add to Home Screen on an iPhone),
`/` asks once for the token, then shows a form to queue a task and the queue
itself: what's running, what's waiting (and when), and recent results. Tap a
task to read its result, edit its prompt, tier, priority or time, run it now,
cancel it, or put it back in the queue. The sign-in is a cookie derived from
the token (HttpOnly, SameSite=Strict, Secure over HTTPS), so changing
`ELVES_TOKEN` signs every browser out.

**As JSON**, with the `Authorization: Bearer` header:

| Request | Does |
|---|---|
| `POST /tasks` `{"prompt", "priority", "tier", "at"}` | queue a task |
| `GET /tasks` | the latest 100 tasks |
| `GET /tasks/<id>` | one task, with its result text |
| `POST /tasks/<id>` `{"action": "save", "prompt", "tier", "priority", "at"}` | edit it (`"at": ""` clears the time) |
| `POST /tasks/<id>` `{"action": "now" \| "cancel" \| "retry"}` | run it within a minute, cancel it, requeue it |

A change to a task a run has already started returns 409.

### SMS intake

Two ways:

- **Through an agent gateway.** If Hermes Agent (or similar) already answers
  your texts, have it run `elf add --source sms "<message>"`.
- **Twilio-style webhook.** Point the number's webhook at
  `https://<your-tunnel>/sms?key=<ELVES_TOKEN>` and list your phone number in
  `web.sms_allow`. Start a message with `!` for priority 1, `opus:` to
  request the frontier tier, `agent:` for the agent tier, or `at 1:30am` to
  schedule it. These can be combined in any order. The same shorthand works
  for prompts posted to `/tasks` without a `tier`.

### Morning digest

Set `[notify].argv` to any command that can deliver text. For iMessage, use
`scripts/imessage.applescript` with the recipient's phone number or Apple ID:

```toml
[notify]
argv = ["osascript", "/Users/YOU/night-elves/scripts/imessage.applescript", "+16125550123", "{text}"]
```

Messages must be signed in to iMessage on the Mac that runs the queue. The
first send from launchd asks for permission to control Messages: approve it
on that Mac's screen (Screen Sharing works), or allow it later in System
Settings > Privacy & Security > Automation. Until then the send fails, the
failure is logged, and the digest is still written to the results folder.

## Backends

| Type | Use for | Cost tracking |
|---|---|---|
| `openai_compat` | any OpenAI-compatible local server | tokens only |
| `anthropic_api` | Claude API key | dollars, from token counts and your prices |
| `command` | any CLI that takes a prompt and prints an answer | runs per night |

A `command` backend passes the prompt as an argument where `{prompt}` appears
in `argv`, or on stdin if it does not. Nothing goes through a shell.
`ANTHROPIC_API_KEY` is removed from its environment, because `claude -p` bills
the API key instead of your subscription when the key is set, and the ledger
would record that spend as $0. Set `env_unset` on the backend to change the
list.

## Agent tier

Most tasks are text in, text out. A task queued with `--tier agent` instead
runs `claude -p` with tools, so it can run commands, write files and fetch
from the network. It is opt-in per task and only runs through the
`claude_agent` backend in `config.example.toml`, which:

- runs in `~/.night-elves/work/<id>/`, and the result file lists what was saved there
- uses `--restricted`, so your user, project and local Claude settings,
  hooks and MCP servers are ignored, and the file tools stay in the workspace
- loads `~/.night-elves/agent-settings.json`, a copy of
  `agent-settings.example.json`: Bash runs in the macOS sandbox with writes
  limited to the workspace, reads of `~/.ssh`, `~/.config`, keychains and the
  night-elves secrets denied, network limited to `allowedDomains`, and no
  fallback to unsandboxed commands
- uses `--permission-mode dontAsk`, so anything not allowed is refused rather
  than waiting for a person
- never sees `ANTHROPIC_API_KEY` or `ELVES_TOKEN`, so it can't bill the API or
  queue tasks for itself
- stops after 200 turns, or after `timeout` seconds (90 minutes), when its
  whole process group is killed
- runs inside the subscription window, up to `max_runs_per_night`

It is set up to build small apps in one go: the allowlist covers PyPI, npm,
crates.io, GitHub and the common front-end CDNs; package caches (`uv`, `pip`,
`npm`, `cargo`) point into the workspace through the backend's `env` table;
`allowLocalBinding` lets it start a dev server and test against it; and the
instructions it gets ask for a README, a passing build and tests, and no
servers left running. The sandbox never lets it write `.git/config` or hooks,
so it can't `git init`; night-elves commits the workspace to git itself when
the task finishes, leaving out caches, `node_modules`, `target` and venvs. It can only use the toolchains already installed
(Node, Python via `uv`, Rust, Swift on the Studio), because it can't install
Homebrew packages. `allowLocalBinding` also lets it reach other services on
localhost, such as LM Studio's API.

On the Studio, `sudo`, writes to the home folder, reads of `~/.ssh` and
connections to unlisted hosts were all refused by the sandbox. A deny rule
alone is not a boundary (`sh -c "sudo …"` gets past it); the sandbox is what
stopped it. Add hosts a task needs to `allowedDomains`.

### Home network

`scripts/allow-lan.py` adds every address on the Mac's local subnets to
`allowedDomains` in `~/.night-elves/agent-settings.json` (the sandbox has no
CIDR syntax, so it's one entry per host; `--remove` takes them out, and a
re-run refreshes them after the network changes). What agent tasks can then do
on the LAN, as tested on the Studio:

- connect to devices over TCP, but only through the sandbox proxy: the private
  ranges are in `NO_PROXY`, so tools must be told to use the proxy
  (`curl --noproxy ''`, or an HTTP CONNECT in scripts)
- read the ARP table (`arp -an`) to see recently active devices
- not ping, use UDP (mDNS/Bonjour, SSDP, SNMP), capture packets, or read
  per-process traffic stats (`nettop`)

So it can find devices and talk to their web pages and APIs, but it can't
measure how much traffic each device sends; that needs your router's stats.

## Limits to know about

- **Only agent-tier tasks use tools.** The local and API backends do not
  run commands or touch files.
- **Subscription usage is not measured.** Neither Claude nor ChatGPT plan
  limits are read; those backends are held back by a time window and a run
  count. Only the API credit is paced in dollars.
- **The ledger only sees its own spend.** If you use the same API key
  elsewhere, lower `monthly_credit_usd` to what you want night-elves to have.
- **The SMS webhook checks a shared secret and a sender allowlist.** It does
  not verify Twilio's request signature.
- **One task at a time.** A lock file stops two runs overlapping, and tasks are
  worked serially. `parent_id` is in the schema for splitting a task into
  subtasks, but the dispatcher does not do that yet.
- **Provider billing rules change.** How `claude -p` and third-party tools are
  billed against subscriptions changed more than once in 2026. Each backend is
  a config block, so repoint or disable one when that happens.

## Tests

```sh
python -m unittest discover -s tests
```
