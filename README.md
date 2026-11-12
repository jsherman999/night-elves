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
cat spec.md | elf add -
elf list
elf show 3
elf budget              # mode, credit spent, tonight's allowance
elf run --dry-run       # show how each queued task would be routed
elf run                 # work the queue now
elf retry 3             # requeue a failed task
```

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

### Web intake

```sh
elf serve               # http://127.0.0.1:8377
curl -H "Authorization: Bearer $ELVES_TOKEN" -H 'Content-Type: application/json' \
     -d '{"prompt":"...","priority":3,"tier":"auto"}' http://127.0.0.1:8377/tasks
```

`GET /` is a small form for a phone. `GET /tasks` lists the queue as JSON. The
server binds to loopback; reach it from elsewhere through Tailscale or a
tunnel instead of opening the port. `launchd/com.nightelves.web.plist` keeps
it running.

### SMS intake

Two ways:

- **Through an agent gateway.** If Hermes Agent (or similar) already answers
  your texts, have it run `elf add --source sms "<message>"`.
- **Twilio-style webhook.** Point the number's webhook at
  `https://<your-tunnel>/sms?key=<ELVES_TOKEN>` and list your phone number in
  `web.sms_allow`. Start a message with `!` for priority 1, or `opus:` to
  request the frontier tier.

### Morning digest

Set `[notify].argv` to any command that can deliver text. For iMessage to
yourself, something like this (not tested here; the Messages scripting syntax
varies between macOS versions):

```toml
argv = ["osascript", "-e", "on run a", "-e",
        'tell application "Messages" to send (item 1 of a) to buddy "+16125550123"',
        "-e", "end run", "{text}"]
```

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

## Limits to know about

- **Tasks are text in, text out.** The built-in local and API backends do not
  use tools or touch files. For tasks that need an agent, make the executor a
  `command` backend that runs one (`claude -p`, `codex exec`, `hermes`).
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
