#!/bin/sh
# Starts the dashboard, plus a bot for every world named in $WORLDS.
#
# Used by both the Docker image and deploy/twb.service, so it has to work in a
# container (system python, /app) and on a host install (./.venv, any path).
#
# WORLDS is optional: leave it empty and start your worlds from the dashboard
# instead (the Start button spawns them as children of the dashboard). Setting it
# means your bots come back automatically after a reboot or restart.
set -e

# Resolve to the project root regardless of where this was invoked from.
cd "$(dirname "$0")/.."

PORT="${PORT:-5000}"

# Prefer the virtualenv from install.sh; in the container there is none and the
# system python already has the dependencies.
if [ -x ".venv/bin/python" ]; then
    PY="$(pwd)/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    PY="python"
fi

# server.py does sys.path.insert(0, "../"), so it only imports correctly when
# its working directory is webmanager/.
(cd webmanager && exec "$PY" server.py "$PORT" 0.0.0.0) &
DASHBOARD_PID=$!
echo "[entrypoint] dashboard listening on 0.0.0.0:$PORT (pid $DASHBOARD_PID)"

for world in ${WORLDS:-}; do
    if [ ! -f "worlds/$world/config.json" ]; then
        echo "[entrypoint] world '$world' has no worlds/$world/config.json yet -" \
             "create it in the dashboard first; skipping"
        continue
    fi
    echo "[entrypoint] starting world $world"
    "$PY" twb.py --world "$world" &
done

# The service's lifetime follows the dashboard: if it dies, the restart policy
# brings the whole thing back up cleanly rather than leaving orphaned bots.
wait "$DASHBOARD_PID"
