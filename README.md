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
| conserve | first half of the cycle | none | local | codex if enabled, else a local model |
| pace | second half | frontier | local | frontier (local if tonight's allowance is gone) |
| endgame | last 7 days | frontier | frontier for priority 1-3, else local | frontier |

In every mode:

- A task with `--tier frontier` goes straight to the frontier model. If there is
  no allowance left tonight it stays queued.
- A task with `--tier agent` goes to an agent with tools, in a sandbox: the local
  agent first if one is set up, else Claude Code (see [Agent tier](#agent-tier)).
  Nothing else is ever routed there. Its work is accepted only when its own check
  passes.
- A task can say when it's done (`--done-when`). The executor, the reviewer and
  the agent are all held to it; without one, the planner writes some.
- A task that fails locally twice is escalated to the frontier model. Failing
  includes a reviewer's FAIL, so local answers get a review even in conserve
  mode: by codex if it's enabled, else by `roles.local_review` (or the local
  model itself).
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
elf add -t agent --done-when "a screenshot of each form is in shots/" \
        "Clone github.com/me/app, run it, and screenshot its forms with Playwright"
elf add --from 9 "Using these 3B2 notes, build a quick-reference page"
cat spec.md | elf add -
elf list
elf show 3
elf budget              # mode, credit spent, tonight's allowance
elf run --dry-run       # show how each queued task would be routed
elf run                 # work the queue now
elf retry 3             # requeue a failed, cancelled or finished task
elf cancel 4 5          # take queued tasks out of the queue
elf edit 4 -t agent -p 2 --at 01:30    # also --prompt, --done-when, --now, --tonight
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
cancel it, or put it back in the queue. Results are shown formatted from their
Markdown (headings, lists, tables, code, links), with a link to the plain text.
The renderer is in `elves/md.py`: it escapes all source text, shows raw HTML as
text, and only links http(s), mailto and same-site paths. The sign-in is a cookie derived from
the token (HttpOnly, SameSite=Strict, Secure over HTTPS), so changing
`ELVES_TOKEN` signs every browser out.

**As JSON**, with the `Authorization: Bearer` header:

| Request | Does |
|---|---|
| `POST /tasks` `{"prompt", "priority", "tier", "at", "criteria", "from"}` | queue a task (`criteria`: done when; `from`: `"9, 12"` or `[9, 12]`) |
| `POST /tasks` as `multipart/form-data`, same fields plus `files` | queue a task with files |
| `GET /tasks` | the latest 100 tasks |
| `GET /tasks/<id>` | one task, with its result text and its files |
| `GET /tasks/<id>/f/<n>` | its nth file (1-based) |
| `POST /tasks/<id>` `{"action": "save", "prompt", "tier", "priority", "at", "criteria"}` | edit it (`"at": ""` clears the time) |
| `POST /tasks/<id>` `{"action": "now" \| "cancel" \| "retry"}` | run it within a minute, cancel it, requeue it |

A change to a task a run has already started returns 409.

### Using an earlier task's result

Every result is a Markdown file, `~/.night-elves/results/<id>.md` (an agent's
other files are in `~/.night-elves/work/<id>/`, listed at the end of that
file). To build on one, reference the task when you queue the next:

- `elf add --from 9 "..."` (repeatable, or `--from 9,12`)
- the web form's "Use results of tasks" box, or `"from": "9, 12"` in JSON
- from the phone or SMS, start the prompt with `from #9:` (or `from #9, #12:`)

Each result is attached to the new task as `task-<id>.md`: a copy taken at
queue time, so rerunning task 9 later doesn't change it. As a text file it is
pasted into the prompt for any tier, and an agent also gets it in `input/`. A
task that has no result yet can't be referenced ("#9 has no result yet"), and
results count towards the 200 KB of text a task can carry inline.

### Files and images

A task can carry up to 20 files of any type: a screenshot to build something
from, a CSV to analyse, a PDF to summarise, a log to dig through. Attach them
with the form's file picker, `elf add -f data.csv -f notes.md "..."` on the
Studio, `curl` from anywhere on the tailnet:

```sh
curl -H "Authorization: Bearer $ELVES_TOKEN" -F 'prompt=Find the outliers' -F tier=agent \
     -F files=@sales.parquet -F files=@notes.md https://<studio>.ts.net/tasks
```

or an iPhone share-sheet Shortcut. `scripts/make-shortcut.py https://<studio>.ts.net`
builds and signs a ready-made one, "Send to Elves" (it needs macOS signed in to
iCloud). Open the file to import it; Shortcuts asks for the token then, so the
file itself holds no secret, and iCloud syncs it to the phone. To build it by hand:

1. New Shortcut, turn on **Show in Share Sheet**, receiving **Images** and **Files**.
2. **Ask for Input** (text): "What should the elves do?"; **List** `agent`, `auto`,
   `frontier` and **Choose from List**.
3. **Get Contents of URL**: `https://<studio>.ts.net/tasks`, method POST,
   header `Authorization: Bearer <ELVES_TOKEN>`, request body **Form** with
   `prompt` = Provided Input, `tier` = Chosen Item, and `files` as a **File**
   field set to Shortcut Input. (`images` is accepted too.)
4. **Get Dictionary Value** `message`, then **Show Notification**. Errors carry
   a `message` too ("Not queued: ..."), so failures show up the same way.

With a bearer token the reply is JSON (`{"id", "files", "message"}`); the
browser form gets the queue page. Uploads are streamed to disk, so size is
limited only by `[web] max_upload_mb` (2048 by default) and free space. A
multipart upload must carry the bearer header or the sign-in cookie, checked
before anything is read.

Each file gets a kind when it arrives, and the kind decides who can work on it:

| Kind | What | Who |
|---|---|---|
| text | UTF-8, up to 200 KB for all of a task's text files together | any tier: pasted into the prompt |
| image | PNG, JPEG, GIF, WebP, HEIC (converted to JPEG; shrunk past 2048 px or 3.75 MB with `sips`) | Claude, or the local vision model |
| pdf | up to 30 MB | Claude, or the local vision model up to 20 pages |
| data | anything else, or text past the 200 KB budget | `agent` only |

**Local vision.** The general local model may not see images (gpt-oss-120b
doesn't). Name a second local backend with `vision = true` in
`roles.local_vision` (see `config.example.toml`) and images, plus PDFs of up
to 20 pages, go to it: `local` uses it, and `auto` uses it first, like the
local model for text, moving to Claude after two failed attempts or for
priority 1-3 tasks in the endgame. There is no planner or reviewer for these,
since they couldn't see the images. Images go as data URLs in the OpenAI
format; PDF pages are rendered to PNG by `elves/pdfpages.swift`, which is
compiled with `swiftc` the first time a PDF arrives (macOS with the Xcode
command line tools) and also counts the pages at intake. Without a vision
backend, `local` refuses images and PDFs and `auto` sends them to Claude.

So without a local vision model, `auto` sends a task with images or PDFs to
the frontier model; with one, it sends it there first. A task
with data files has to be queued as `agent` (the agent tier stays opt-in, so
it isn't chosen for you). The agent gets copies in `work/<id>/input/`, made
with APFS clones so a big file isn't stored twice and the original stays
untouched. The frontier model gets images and PDFs inline through the API, or,
on the `claude -p` fallback, as files in the attachments folder it runs in.

Files are stored in `~/.night-elves/attachments/<id>/` under generated names
(`2-sales.csv`: the position, then the upload's name with anything outside
`A-Z a-z 0-9 . _ -` replaced), and listed on the task's page. Images show as
thumbnails; images, PDFs and text open in the browser, and everything else
(HTML and SVG included, which are treated as text or data) is shown as plain
text or downloaded, never run. SMS intake is text only.

### SMS intake

Two ways:

- **Through an agent gateway.** If Hermes Agent (or similar) already answers
  your texts, have it run `elf add --source sms "<message>"`.
- **Twilio-style webhook.** Point the number's webhook at
  `https://<your-tunnel>/sms?key=<ELVES_TOKEN>` and list your phone number in
  `web.sms_allow`. Start a message with `!` for priority 1, `opus:` to
  request the frontier tier, `agent:` for the agent tier, `at 1:30am` to
  schedule it, or `from #9:` to attach task 9's result. These can be combined
  in any order. The same shorthand works
  for prompts posted to `/tasks` without a `tier`.

### Morning digest

Set `[notify].argv` to any command that can deliver text. For iMessage, use
`scripts/imessage.applescript` with the recipient's phone number or Apple ID:

```toml
[notify]
argv = ["osascript", "/Users/YOU/night-elves/scripts/imessage.applescript", "+16125550123", "{text}"]
```

Set `[web].public_url` to the address the phone pages are reached at (your
Tailscale name, for example) and each finished or failed task in the digest
gets a link to its result page.

Messages must be signed in to iMessage on the Mac that runs the queue. If it
is signed in to your own Apple ID and the recipient is you, the digest lands
in your conversation with yourself, which iOS doesn't notify for. The
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
runs an agent with tools, so it can run commands, write files and fetch from
the network. It is opt-in per task. Two agents can take it:

- **The local agent** (`local_agent` in `config.example.toml`, used when
  `roles.local_agent` names it): opencode driving a local model (Qwen3.8-27B on
  the Studio), inside Anthropic's sandbox runtime. It runs any time, so an agent
  task set to run now during the day starts within a minute. It goes first.
- **Claude Code** (`claude_agent`): takes a task after the local agent has
  failed it twice (`escalate_after`), and takes priority 1-3 tasks
  (`hard_priority`) from the start. Without a local agent it takes them all.

The `claude_agent` backend:

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

### Done means checked

An agent doesn't get to declare itself finished. Its instructions end with a
contract: the last two lines of its reply must be

```
STATUS: DONE            (or INCOMPLETE)
CHECK: <one shell command that exits 0 only when the work is right>
```

When the agent exits, night-elves runs the `CHECK` command itself, from the
workspace, under `sandbox-exec` with its own profile: writes only in the
workspace and the temp folders, no network except localhost, no secrets, and
`sudo`, `osascript`, `security`, `launchctl` and `ssh` refused. It gets
`check_timeout` seconds (10 minutes), and anything it leaves running is killed.
The work is accepted only on `STATUS: DONE` and a check that exits 0. A missing
STATUS line, `INCOMPLETE`, or a failing check counts as a failed attempt, and the
next attempt is told why, including the end of the check's output. The task's
`--done-when` text, if it has one, is in the agent's instructions too, and the
check should test it. `CHECK: none` is accepted, for tasks that only gather
information; the result says so.

The result file has a **Check** section with the command, its exit code and
the end of its output. A rejected attempt's result is kept too, marked "not
accepted", and the workspace is committed to git after every attempt, so you
can see what each one did.

### Retries continue the session

A retry doesn't start over. Each agent task keeps a session id
(`session_args`/`resume_args` on the backend): Claude Code gets `--session-id`,
then `--resume`, and opencode gets `--session ses_…`, which creates the session
the first time and continues it after. So an agent that hit its time limit or
turn limit, or whose check failed, carries on with everything it knew, and is
told what went wrong. If the session has gone (Claude prints "No conversation
found"), it starts a new one in the same workspace, told that the earlier files
are there. A task that moves from the local agent to Claude starts a Claude
session, with the same workspace and the same feedback.

### Setting up the local agent

On the Studio this needs opencode, LM Studio with the model loaded, and
Anthropic's [sandbox runtime](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime):

```sh
npm i -g @anthropic-ai/sandbox-runtime          # the srt command
cp srt-settings.example.json ~/.night-elves/srt-settings.json
cp opencode.example.json ~/.night-elves/opencode.json
```

Then copy the `[backends.local_agent]` block from `config.example.toml` and set
`roles.local_agent = "local_agent"`. `srt-settings.json` matches the sandbox in
`agent-settings.example.json`: writes only in the workspace (`"."`, as srt runs
there), the same denied reads, and the same domain allowlist; keep the two in
step. opencode runs with `--standalone`, so its tools run in srt with it rather
than in opencode's background service, and with `--auto`, as nobody is there to
approve anything. Its config, sessions and caches are pointed into the
workspace by the backend's `env` table.

### Browsers

Playwright's Chromium is installed for the agents to use
(`npx playwright@1.65.0 install chromium`, into `~/Library/Caches/ms-playwright`).
Agents can't download a browser themselves, so their `notes` (added to their
instructions) tell them to use that exact Playwright version, and to launch
Chromium with `--single-process`: inside the sandbox it can't register the Mach
port its helper processes need, and fails with `bootstrap_check_in ...
Permission denied` otherwise. The check sandbox doesn't have that limit.

Images an agent saves in its workspace (screenshots, say) are shown on the
task's page, up to 24 of them; hidden folders, `node_modules` and the like are
left out, and only PNG, JPEG, GIF and WebP files are served.

### Home network

`scripts/allow-lan.py` adds every address on the Mac's local subnets to
`allowedDomains` in `~/.night-elves/agent-settings.json`, and in
`srt-settings.json` for the local agent if it's there (the sandbox has no
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
  run commands or touch files. A reviewer only reads the answer; only an
  agent's check actually runs anything.
- **A check is only as good as the agent makes it.** night-elves runs it and
  believes its exit code; a weak check (`CHECK: test -f README.md`) passes weak
  work. `--done-when` is the way to say what the check should prove.
- **Subscription usage is not measured.** Neither Claude nor ChatGPT plan
  limits are read; those backends are held back by a time window and a run
  count. Only the API credit is paced in dollars.
- **The ledger only sees its own spend.** If you use the same API key
  elsewhere, lower `monthly_credit_usd` to what you want night-elves to have.
- **The SMS webhook checks a shared secret and a sender allowlist.** It does
  not verify Twilio's request signature.
- **One task at a time.** A lock file stops two runs overlapping (the nightly run
  waits for a daytime one, such as a long local agent task, to finish), and tasks are
  worked serially. `parent_id` is in the schema for splitting a task into
  subtasks, but the dispatcher does not do that yet.
- **Provider billing rules change.** How `claude -p` and third-party tools are
  billed against subscriptions changed more than once in 2026. Each backend is
  a config block, so repoint or disable one when that happens.

## Tests

```sh
python -m unittest discover -s tests
```
