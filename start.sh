#!/usr/bin/env bash
# Starts one or more worlds and the shared web panel in one tmux session.
#
# Usage: ./start.sh [world ...]
#   world   zero or more world names. Each runs `python3 twb.py --world <name>`
#           out of worlds/<name>/. With no world it runs the default bot
#           (python3 twb.py, root config.json) - exactly like before.
#
# Environment:
#   PORT    web panel port (default 5000)
#   HOST    web panel bind address (default 0.0.0.0; use 127.0.0.1 for local-only)
#
# Examples:
#   ./start.sh                 # default world + panel (unchanged behaviour)
#   ./start.sh nl98           # the nl98 world + panel
#   ./start.sh nl99 nl98     # two worlds + one shared panel
#   PORT=5001 ./start.sh nl98 # panel on port 5001

SESSION="twb"
PORT="${PORT:-5000}"
HOST="${HOST:-0.0.0.0}"

# Worlds to launch; an empty entry ("") means the default (no --world) bot.
WORLDS=("$@")
[ ${#WORLDS[@]} -eq 0 ] && WORLDS=("")

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' is already running, attaching..."
    exec tmux attach -t "$SESSION"
fi

# Build the bot command for a world ("" = default).
botcmd() {
    if [ -n "$1" ]; then echo "python3 twb.py --world $1"; else echo "python3 twb.py"; fi
}

# Integrity-check each world before launching.
for w in "${WORLDS[@]}"; do
    echo "Verifying bot integrity${w:+ for world $w}"
    if ! $(botcmd "$w") -i; then
        echo "It looks like the bot failed to start${w:+ for world $w}."
        echo "Please re-check the config or re-install the bot, and see"
        echo "https://github.com/stefan2200/TWB/issues if it persists."
        exit 1
    fi
done

# First world creates the session window; the rest get their own panes.
tmux new-session -d -s "$SESSION" -n bot "$(botcmd "${WORLDS[0]}")"
for w in "${WORLDS[@]:1}"; do
    tmux split-window -t "$SESSION:bot" "$(botcmd "$w")"
    tmux select-layout -t "$SESSION:bot" tiled >/dev/null
done

# One shared web panel for all worlds.
tmux split-window -t "$SESSION:bot" "cd webmanager && python3 server.py $PORT $HOST"
tmux select-layout -t "$SESSION:bot" tiled >/dev/null

exec tmux attach -t "$SESSION"
