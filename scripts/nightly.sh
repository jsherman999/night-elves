#!/bin/sh
# Wrapper for launchd: loads secrets, keeps the Mac awake, runs the queue.
# Secrets live in ~/.night-elves/env (chmod 600), e.g.
#   export ANTHROPIC_API_KEY=...
#   export ELVES_TOKEN=...
set -eu
[ -f "$HOME/.night-elves/env" ] && . "$HOME/.night-elves/env"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"  # pipx puts elf in ~/.local/bin
exec caffeinate -i elf run
