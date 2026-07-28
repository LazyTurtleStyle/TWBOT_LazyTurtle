#!/usr/bin/env bash
# Starts one or more worlds and the shared web panel in one tmux session.
#
# Usage: ./start.sh [world ...]
#   world   zero or more world names. Each runs `python3 twb.py --world <name>`
#           out of worlds/<name>/. With no world it runs the default bot
#           (python3 twb.py, root config.json), or - if there is no root
#           config.json and exactly one world under worlds/ is set up - that
#           world, so you cannot accidentally set the same account up twice.
#
# Environment:
#   PORT    web panel port (default 5000)
#   HOST    web panel bind address (default 0.0.0.0; use 127.0.0.1 for local-only)
#
# Examples:
#   ./start.sh                 # default world + panel (unchanged behaviour)
#   ./start.sh nl98            # the nl98 world + panel
#   ./start.sh nl99 nl98       # two worlds + one shared panel
#   PORT=5001 ./start.sh nl98  # panel on port 5001

SESSION="twb"
PORT="${PORT:-5000}"
HOST="${HOST:-0.0.0.0}"

cd "$(dirname "$0")"

# Prefer the virtualenv created by install.sh; fall back to a system python3 so
# existing setups that pip-installed globally keep working unchanged.
if [ -x ".venv/bin/python" ]; then
    PY="$(pwd)/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "No Python found. Run ./install.sh first."
    exit 1
fi

# Worlds to launch; an empty entry ("") means the default (no --world) bot.
WORLDS=("$@")
if [ ${#WORLDS[@]} -eq 0 ]; then
    # No world named. A top-level config.json means this is a single-world
    # install - run it, unchanged. Otherwise use the world that is already set
    # up under worlds/ rather than starting the default bot, which would walk
    # into the first-run setup wizard and configure the same account a second
    # time (two setups of one account = two bots logging each other out).
    configured=()
    if [ ! -f config.json ]; then
        for cfg in worlds/*/config.json; do
            [ -f "$cfg" ] || continue
            configured+=("$(basename "$(dirname "$cfg")")")
        done
    fi
    case ${#configured[@]} in
        0) WORLDS=("") ;;   # nothing set up yet, or a single-world install
        1) WORLDS=("${configured[0]}")
           echo "Using the only world that is set up: ${configured[0]}" ;;
        *) echo "Several worlds are set up under worlds/: ${configured[*]}"
           echo "Say which one(s) to start, for example:"
           echo "  ./start.sh ${configured[0]}"
           exit 1 ;;
    esac
fi

# Named worlds must already be set up. Without this a typo (nl99 -> n199) is
# indistinguishable from a new world: the bot creates worlds/<typo>/, finds no
# config and waits for a world nobody is setting up, while the log stays empty.
# Checked before the attach-to-running-session branch below, or a typo would
# silently drop you into the session that is already running instead.
for w in "${WORLDS[@]}"; do
    [ -n "$w" ] || continue
    [ -f "worlds/$w/config.json" ] && continue
    echo "There is no world called '$w': worlds/$w/config.json does not exist."
    have=()
    for cfg in worlds/*/config.json; do
        [ -f "$cfg" ] && have+=("$(basename "$(dirname "$cfg")")")
    done
    if [ ${#have[@]} -gt 0 ]; then
        echo "Worlds set up here: ${have[*]}"
        echo "Check the spelling, for example:  ./start.sh ${have[0]}"
    else
        echo "Open the dashboard and use 'Add world' to set one up first."
    fi
    exit 1
done

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' is already running, attaching..."
    exec tmux attach -t "$SESSION"
fi

# Build the bot command for a world ("" = default).
botcmd() {
    if [ -n "$1" ]; then echo "$PY twb.py --world $1"; else echo "$PY twb.py"; fi
}

# Integrity-check each world before launching.
for w in "${WORLDS[@]}"; do
    echo "Verifying bot integrity${w:+ for world $w}"
    if ! $(botcmd "$w") -i; then
        echo "It looks like the bot failed to start${w:+ for world $w}."
        echo "Please re-check the config or re-run ./install.sh, and see"
        echo "https://github.com/LazyTurtleStyle/TWBOT_LazyTurtle/issues if it persists."
        exit 1
    fi
done

# Stop bots for these worlds still running outside tmux (web-panel starts,
# leftovers from a killed session) so they don't run twice.
for w in "${WORLDS[@]}"; do
    if [ -n "$w" ]; then
        pkill -f "twb\.py --world $w\$" 2>/dev/null
    else
        pkill -f "twb\.py\$" 2>/dev/null
    fi
done

# Kill any stray web panel left over from manual restarts, or the new one dies
# with "address already in use".
pkill -f "server\.py $PORT" 2>/dev/null && sleep 1

# Preferred layout: everything in one tmux session, one pane per process, so you
# can watch the bots and detach with Ctrl-B D while they keep running.
if command -v tmux >/dev/null 2>&1; then
    # First world creates the session window; the rest get their own panes.
    tmux new-session -d -s "$SESSION" -n bot "$(botcmd "${WORLDS[0]}")"
    for w in "${WORLDS[@]:1}"; do
        tmux split-window -t "$SESSION:bot" "$(botcmd "$w")"
        tmux select-layout -t "$SESSION:bot" tiled >/dev/null
    done

    # One shared web panel for all worlds.
    tmux split-window -t "$SESSION:bot" "cd webmanager && $PY server.py $PORT $HOST"
    tmux select-layout -t "$SESSION:bot" tiled >/dev/null

    exec tmux attach -t "$SESSION"
fi

# No tmux (minimal VPS, fresh Raspberry Pi, macOS without Homebrew): run the same
# processes detached with nohup and log to cache/logs/. tmux is nicer - install it
# with `sudo apt install tmux` / `brew install tmux` - but this works everywhere.
echo "tmux is not installed - starting in the background instead."
mkdir -p cache/logs
PIDFILE="cache/twb.pids"
: > "$PIDFILE"

for w in "${WORLDS[@]}"; do
    logname="cache/logs/bot${w:+-$w}.out"
    nohup $(botcmd "$w") >> "$logname" 2>&1 &
    echo $! >> "$PIDFILE"
    echo "  bot${w:+ $w} started (pid $!), output: $logname"
done

nohup sh -c "cd webmanager && exec $PY server.py $PORT $HOST" >> cache/logs/panel.out 2>&1 &
echo $! >> "$PIDFILE"
echo "  dashboard started (pid $!), output: cache/logs/panel.out"

echo ""
echo "Dashboard: http://localhost:$PORT/"
echo "Follow the log:  tail -f cache/logs/bot*.out"
echo "Stop everything: kill \$(cat $PIDFILE)"
