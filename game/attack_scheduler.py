"""
Scheduled (timed) attacks.

The Attack tab lets you queue an attack to land at a chosen time; this module
stores those commands and fires them. Scheduling is by *arrival* time: the send
moment is arrival - travel_time, computed when the command is created. A
background thread in the bot process (twb.py) watches the queue and runs the
send when each command is due.

Precision is "best effort": when a command is due the whole open -> confirm ->
launch request sequence runs, which itself takes a few hundred ms. A small lead
offset (sched_lead_seconds) starts the sequence slightly early to compensate.
This is good for coordinated landings within ~a second, not frame-perfect snipes.

The queue file is shared by two processes (the bot writes statuses, the web
dashboard appends/cancels), so every read-modify-write goes through update():
a cross-process file lock around an atomic replace. Due commands are *claimed*
(pending -> sending) under that lock before sending, so a command is launched by
exactly one caller even if two run at once.
"""
import json
import logging
import os
import random
import time
import uuid

from core.extractors import Extractor
from core.filemanager import FileManager

try:
    import fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover
    fcntl = None

SCHEDULE_FILE = "cache/scheduled_attacks.json"

# Units the rally point form accepts, in a stable order.
UNIT_KEYS = [
    "spear", "sword", "axe", "archer", "spy", "light", "marcher",
    "heavy", "ram", "catapult", "knight", "snob",
]

logger = logging.getLogger("AttackScheduler")


def _resolve(path):
    """Absolute path of the queue file. None -> the bot's world-aware default."""
    return path or FileManager.get_path(SCHEDULE_FILE)


def _read(path):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    commands = data.get("commands") if isinstance(data, dict) else None
    return commands if isinstance(commands, list) else []


def _write_atomic(path, commands):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"commands": commands}, f, indent=2, sort_keys=False)
    os.replace(tmp, path)  # atomic on POSIX, so readers never see a partial file


class _Lock:
    """Cross-process exclusive lock on <path>.lock (no-op without fcntl)."""

    def __init__(self, path):
        self.lockpath = path + ".lock"
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        os.makedirs(os.path.dirname(self.lockpath), exist_ok=True)
        self.fh = open(self.lockpath, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def update(mutator, path=None):
    """Atomically read -> mutate -> write the queue under the cross-process lock.

    `mutator(commands)` edits the list in place; its return value is passed back
    to the caller (e.g. the claimed commands, or a changed flag)."""
    path = _resolve(path)
    with _Lock(path):
        commands = _read(path)
        result = mutator(commands)
        _write_atomic(path, commands)
    return result


def load_schedule(path=None):
    """Current queue (always a list). Reads are lock-free: writes are atomic."""
    return _read(_resolve(path))


def add_command(entry, path=None):
    """Append a new scheduled command and return it (with an assigned id)."""
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("status", "pending")
    entry.setdefault("created", int(time.time()))
    update(lambda commands: commands.append(entry), path)
    return entry


def cancel_command(command_id, path=None):
    """Mark a pending command cancelled. Returns True if one was changed."""
    def mut(commands):
        changed = False
        for c in commands:
            if c.get("id") == command_id and c.get("status") == "pending":
                c["status"] = "cancelled"
                c["finished"] = int(time.time())
                changed = True
        return changed
    return update(mut, path)


def prune(max_age_done=86400, path=None):
    """Drop finished commands older than max_age_done so the file stays small."""
    now = int(time.time())

    def mut(commands):
        kept = [
            c for c in commands
            if c.get("status") in ("pending", "sending")
            or now - int(c.get("finished", c.get("created", now))) < max_age_done
        ]
        commands[:] = kept
    update(mut, path)


def next_send_ts(path=None):
    """Earliest send_ts among pending commands, or None when the queue is idle."""
    pending = [c for c in load_schedule(path) if c.get("status") == "pending"]
    if not pending:
        return None
    return min(float(c.get("send_ts", 0)) for c in pending)


def claim_due(path=None, lead=0.0, now=None):
    """Atomically move every due pending command to 'sending' and return them, so
    a command is claimed by exactly one caller (no double-send).

    A command already in 'sending' whose claim is older than STALE_SENDING_SECONDS
    is treated as abandoned (the process that claimed it crashed mid-launch) and
    is reclaimed too — otherwise it would stay 'sending' forever, never retried
    (only 'pending' is normally claimed) and never pruned."""
    now = now if now is not None else time.time()
    claimed = []

    def mut(commands):
        for c in commands:
            status = c.get("status")
            due = float(c.get("send_ts", 0)) - lead <= now
            stale = (
                status == "sending"
                and now - int(c.get("claimed_at", 0)) > STALE_SENDING_SECONDS
            )
            if (status == "pending" and due) or stale:
                c["status"] = "sending"
                c["claimed_at"] = int(time.time())
                claimed.append(dict(c))  # copy: caller reads it after the lock releases
    update(mut, path)
    return claimed


def _set_status(command_id, status, path=None, **extra):
    def mut(commands):
        for c in commands:
            if c.get("id") == command_id:
                c["status"] = status
                c["finished"] = int(time.time())
                c.update(extra)
    update(mut, path)


# How early (seconds) to claim a command and run the open+confirm steps before
# its send moment, so only the final fast launch request remains to time. Must
# comfortably exceed the open+confirm round-trips.
PRESTAGE_SECONDS = 15
# A command legitimately sits in 'sending' only for the brief pre-stage + launch
# window (a few seconds beyond PRESTAGE_SECONDS). Past this many seconds it must
# be a leftover from a crashed launch, so claim_due may reclaim and retry it.
STALE_SENDING_SECONDS = 120
# Compensation (seconds) for the launch request's own one-way latency: fire this
# much before the computed launch moment so troops actually leave on time. Tune
# to roughly your ping to the game server (observed near-zero on a fast host, so
# 0.0 lands on target; raise it if attacks land late, lower if they land early).
NETWORK_LEAD = 0.0
# Human-pacing gap (seconds) inserted BETWEEN the open and confirm prep steps of a
# timed send. Timed sends run on a priority_mode wrapper, which strips the normal
# 3-7s pacing so the final launch can fire instantly; without this, open+confirm
# would hit the server back-to-back with only network latency between them - an
# unnatural signature bot detection watches for on the (heavily-scrutinised)
# attack path. This gap runs entirely inside the pre-stage window, well before the
# launch, so it NEVER affects arrival accuracy - only the launch request is
# time-critical. Keep the max comfortably under PRESTAGE_SECONDS.
PREP_JITTER_MIN = 0.4
PREP_JITTER_MAX = 1.8


def prepare_command(wrapper, origin_id, x, y, units):
    """Open the rally point and run the confirm step, but do NOT launch yet.

    Returns (confirm_data, server_duration, error). confirm_data is the ready-to
    -launch form; server_duration is TribalWars' own travel time for this command
    in seconds (the authoritative figure for hitting an exact arrival)."""
    units = {u: int(n) for u, n in (units or {}).items() if int(n or 0) > 0}
    if not units:
        return None, 0, "no units selected"

    # 1) Open the rally point to collect the form's hidden fields + token.
    open_url = f"game.php?village={origin_id}&screen=place&target_type=coord"
    pre = wrapper.get_url(open_url)
    if not pre:
        return None, 0, "could not open rally point"
    pre_data = {k: v for k, v in Extractor.attack_form(pre)}
    pre_data.update({str(u): str(n) for u, n in units.items()})
    pre_data.update({"x": x, "y": y, "target_type": "coord", "attack": "Aanvallen"})

    # Human-pacing gap between opening the rally point and confirming it. Only
    # needed when the wrapper is in priority_mode (timed sends), where its built-in
    # 3-7s pacing is off - a normal wrapper already spaces these two requests. This
    # runs during the pre-stage window, so it does not delay the launch.
    if getattr(wrapper, "priority_mode", False):
        time.sleep(random.uniform(PREP_JITTER_MIN, PREP_JITTER_MAX))

    # 2) Confirm step: returns the launch form (fresh token), the server's exact
    # travel duration, and an error box if the target/troops are invalid.
    confirm_url = f"game.php?village={origin_id}&screen=place&try=confirm"
    conf = wrapper.post_url(url=confirm_url, data=pre_data)
    if not conf or '<div class="error_box">' in conf.text:
        return None, 0, "rally point rejected the command (bad target or no troops)"

    duration = Extractor.attack_duration(conf)
    confirm_data = {}
    for k, v in Extractor.attack_form(conf):
        if k == "support":
            continue
        confirm_data[k] = v
    confirm_data.update({"building": "main", "h": wrapper.last_h})
    if "x" not in confirm_data:
        confirm_data["x"] = x
    return confirm_data, duration, None


def fire_command(wrapper, origin_id, confirm_data):
    """Send the final launch request for an already-prepared command."""
    result = wrapper.get_api_action(
        village_id=origin_id,
        action="popup_command",
        params={"screen": "place"},
        data=confirm_data,
    )
    return (True, "sent") if result else (False, "launch request failed")


def send_command(wrapper, origin_id, x, y, units):
    """Immediate (untimed) send: prepare then fire back-to-back."""
    confirm_data, _duration, err = prepare_command(wrapper, origin_id, x, y, units)
    if err:
        return False, err
    return fire_command(wrapper, origin_id, confirm_data)


def execute_timed(wrapper, command, network_lead=NETWORK_LEAD):
    """Prepare a command, then fire it so the troops LAND at command['arrival_ts'].

    The wait is computed from the server's reported duration (not our estimate),
    so arrival accuracy is limited only by the final request's latency/jitter,
    not by travel-time rounding. Returns (ok, message)."""
    confirm_data, duration, err = prepare_command(
        wrapper, command.get("origin_id"), command.get("target_x"),
        command.get("target_y"), command.get("units") or {})
    if err:
        return False, err

    arrival = float(command.get("arrival_ts", 0))
    if duration > 0 and arrival > 0:
        launch_at = arrival - duration - network_lead
        wait = launch_at - time.time()
        if wait > 0:
            time.sleep(wait)
        elif wait < -2:
            # We were claimed too late to hit the window (e.g. prepare was slow);
            # send anyway but report how far off the launch is.
            logger.warning("Scheduled attack %s launching %.1fs late",
                           command.get("id"), -wait)
    ok, msg = fire_command(wrapper, command.get("origin_id"), confirm_data)
    if ok:
        msg = "sent (server travel %ds)" % duration
    return ok, msg


def run_due(wrapper, lead=PRESTAGE_SECONDS, network_lead=NETWORK_LEAD, path=None):
    """Claim and fire every command entering its pre-stage window. Each is claimed
    (pending -> sending) atomically so it fires exactly once, prepared, fired at
    the precise moment, then marked sent/failed. Returns the number sent.

    Claimed commands are handled soonest-first; note they fire sequentially, so
    several commands due within the same prestage window are not frame-accurate
    relative to each other (fine for spaced-out commands, not noble trains)."""
    sent = 0
    claimed = sorted(claim_due(path=path, lead=lead),
                     key=lambda c: float(c.get("send_ts", 0)))
    for c in claimed:
        cid = c.get("id")
        try:
            ok, msg = execute_timed(wrapper, c, network_lead=network_lead)
        except Exception as exc:  # never let one bad command kill the loop
            ok, msg = False, "exception: %s" % exc
        _set_status(cid, "sent" if ok else "failed", path=path,
                    result=msg, sent_at=int(time.time()))
        logger.info(
            "Scheduled attack %s %s -> %s|%s: %s",
            cid, c.get("origin_id"), c.get("target_x"), c.get("target_y"), msg,
        )
        if ok:
            sent += 1
    return sent
