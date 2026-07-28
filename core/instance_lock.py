"""Single-instance guard: one bot process per TribalWars account.

Two twb.py processes pointed at the same account do not just run twice, they
fight over the session. TribalWars rotates the session id; each process writes
the rotated cookies to its own cache/session.json and re-auths from cookies.txt
whenever it lands on a login page, so every rotation invalidates the other
process's id. The result is one bot that keeps playing normally and one that
prints

    Overview could not be read: the session looks logged out (cookie expired)

every cycle, forever. Nothing else looks wrong: the winner's actions still show
up in-game and in the dashboard's bot log, so the install looks healthy while
half of it is dead and a restart "fixes" it only until the next double start.

The lock is keyed on the game endpoint rather than on the --world name on
purpose. A bot started without --world (project-root config.json) and one
started with `--world nl115` are two different worlds to the dashboard but the
same account to TribalWars - which is exactly how a second instance gets
launched by accident (start.bat opens one, the dashboard's "Start bot" button
does not recognise it, and opens another).

Lock files live in <project root>/cache/locks/ - the code root, never the
per-world data dir, so instances with different data dirs can see each other.
"""

import atexit
import json
import os
import time
from urllib.parse import urlparse

import psutil

from core.filemanager import FileManager


class InstanceLock:
    # Locks acquired by this process, key -> path, released on exit.
    _held = {}

    @staticmethod
    def _lock_dir():
        return os.path.join(FileManager.get_root(), "cache", "locks")

    @staticmethod
    def key_for(endpoint):
        """A filesystem-safe account key: the game host, e.g. nl115.tribalwars.nl.

        Falls back to a sanitised copy of whatever was passed when the endpoint
        is not a parsable URL, so a malformed config still gets *a* lock rather
        than none.
        """
        host = urlparse(str(endpoint or "")).netloc or str(endpoint or "")
        safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in host)
        return safe.strip("._-") or "default"

    @staticmethod
    def _lock_path(key):
        return os.path.join(InstanceLock._lock_dir(), "%s.pid" % key)

    @staticmethod
    def _create_time(pid):
        """Process start time, or None if the pid is gone/unreadable."""
        try:
            return psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            return None

    @staticmethod
    def _holder_alive(info):
        """True if the lock's owner process is still running.

        A live pid alone is not proof: pids get reused, and a lock left by a
        killed bot would then block every future start. The recorded start time
        pins the lock to that exact process. When it cannot be read (permissions)
        we assume the owner is alive rather than steal a lock from a live bot.
        """
        pid = info.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            if not psutil.pid_exists(pid):
                return False
        except OSError:
            return True
        started = InstanceLock._create_time(pid)
        recorded = info.get("create_time")
        if started is None or not recorded:
            return True
        return abs(started - float(recorded)) < 1.0

    @staticmethod
    def holder(endpoint):
        """The live lock holder for an account, or None.

        Returns {'pid', 'world', 'endpoint', 'since'}. A lock left behind by a
        killed process (pid gone, or reused by something that is not a bot) is
        removed here, so a crashed bot never needs manual cleanup.
        """
        path = InstanceLock._lock_path(InstanceLock.key_for(endpoint))
        try:
            with open(path, encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            return None
        if not InstanceLock._holder_alive(info):
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return info

    @staticmethod
    def acquire(endpoint, world=None):
        """Claim the account lock. Returns None on success, else the holder info.

        Re-acquiring from the same process (twb.py's crash-retry loop) succeeds.
        """
        key = InstanceLock.key_for(endpoint)
        existing = InstanceLock.holder(endpoint)
        if existing and existing.get("pid") != os.getpid():
            return existing
        os.makedirs(InstanceLock._lock_dir(), exist_ok=True)
        path = InstanceLock._lock_path(key)
        payload = {
            "pid": os.getpid(),
            "create_time": InstanceLock._create_time(os.getpid()),
            "world": world or "",
            "endpoint": str(endpoint or ""),
            "since": int(time.time()),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        if key not in InstanceLock._held:
            InstanceLock._held[key] = path
            atexit.register(InstanceLock.release, endpoint)
        return None

    @staticmethod
    def release(endpoint):
        """Drop this process's lock (no-op if it is held by someone else)."""
        key = InstanceLock.key_for(endpoint)
        path = InstanceLock._held.pop(key, None) or InstanceLock._lock_path(key)
        try:
            with open(path, encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            return
        if info.get("pid") == os.getpid():
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def describe(info):
        """One-line description of a lock holder for user-facing messages."""
        world = info.get("world") or "the default world"
        age = max(0, int(time.time()) - int(info.get("since") or 0))
        return "pid %s (world: %s, running for %d min)" % (
            info.get("pid"), world, age // 60)
