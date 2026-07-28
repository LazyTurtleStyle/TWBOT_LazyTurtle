#!/usr/bin/env bash
# One-time setup for Linux, macOS and Raspberry Pi.
#
# Creates a private virtualenv in .venv/ and installs the dependencies there,
# so nothing is installed system-wide and your distro's Python stays untouched.
# Safe to re-run: it upgrades the dependencies of an existing .venv.
#
# Usage: ./install.sh
# Then:  ./start.sh            (single world)
#        ./start.sh nl99      (a named world)

set -euo pipefail
cd "$(dirname "$0")"

MIN_MINOR=10  # Python 3.10+

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Find a usable Python -----------------------------------------------------
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; then
        PY="$cand"
        break
    fi
done

if [ -z "$PY" ]; then
    say "No Python 3.$MIN_MINOR or newer was found."
    say ""
    say "Install it with one of:"
    say "  Debian/Ubuntu/Raspberry Pi OS:  sudo apt install python3 python3-venv python3-pip"
    say "  Fedora:                         sudo dnf install python3 python3-pip"
    say "  Arch:                           sudo pacman -S python python-pip"
    say "  macOS (Homebrew):               brew install python"
    exit 1
fi
say "Using $("$PY" -V) at $(command -v "$PY")"

# --- Create / reuse the virtualenv --------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
    say "Creating virtualenv in .venv/"
    if ! "$PY" -m venv .venv 2>/dev/null; then
        say ""
        say "Could not create the virtualenv. On Debian/Ubuntu/Raspberry Pi OS the"
        say "venv module ships separately - install it and re-run this script:"
        say "  sudo apt install python3-venv"
        exit 1
    fi
else
    say "Reusing existing virtualenv in .venv/"
fi

VPY=".venv/bin/python"

# --- Dependencies -------------------------------------------------------------
say "Installing dependencies (this can take a minute on a Raspberry Pi)"
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install --upgrade -r requirements.txt

# --- Verify -------------------------------------------------------------------
say "Verifying bot integrity"
if ! "$VPY" twb.py -i; then
    die "The bot failed its integrity check. See the output above, or open an issue at https://github.com/LazyTurtleStyle/TWBOT_LazyTurtle/issues"
fi

chmod +x start.sh 2>/dev/null || true

say ""
say "Done. Start the bot with:"
say "  ./start.sh                 # single world (config.json in this folder)"
say "  ./start.sh nl99            # a named world, data under worlds/nl99/"
say "  ./start.sh nl99 nl98       # two worlds + one shared dashboard"
say ""
say "The dashboard then runs on http://localhost:5000/ - see README.md to reach"
say "it from your phone or another PC on the same network."
