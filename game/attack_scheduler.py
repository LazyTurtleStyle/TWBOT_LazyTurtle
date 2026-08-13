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

A command may carry `waves`: several unit splits that must leave together, which
is how a noble train is sent (one noble per wave, so the loyalty drops in one
uninterruptible sequence). Every wave is prepared during the pre-stage window and
only the launch requests are left for the send moment, so the waves leave a
single round-trip apart rather than a whole rally-point sequence apart.

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
import re
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

# Extra pre-stage seconds per follow-up wave: every wave needs its own
# open+confirm round trip (plus the human-pacing gap between them) before any of
# them can be fired, and all of that has to be done before the launch moment.
PRESTAGE_PER_WAVE = 12


def command_prestage(command, lead=PRESTAGE_SECONDS):
    """Seconds before its send moment that this command must be claimed.

    A train resolved at send time may come out a wave or two bigger than it
    looked when it was queued (a noble finished in the meantime), so it budgets
    for `train.max_waves` - and never grows past what was budgeted here, or the
    extra open+confirm round trips would push the launch past its moment.
    """
    spec = command.get("train") or {}
    planned = max(len(command.get("waves") or []), int(spec.get("max_waves") or 0))
    return lead + max(0, planned - 1) * PRESTAGE_PER_WAVE


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


def next_send_ts(path=None, lead=PRESTAGE_SECONDS):
    """Earliest send moment among pending commands, or None when idle.

    A train needs a longer pre-stage than a single command (one open+confirm per
    wave), so its send moment is reported early by exactly that difference -
    that way a caller that wakes `lead` seconds before this still has time to
    prepare every wave.
    """
    pending = [c for c in load_schedule(path) if c.get("status") == "pending"]
    if not pending:
        return None
    return min(float(c.get("send_ts", 0)) - (command_prestage(c, lead) - lead)
               for c in pending)


def claim_due(path=None, lead=0.0, now=None):
    """Atomically move every due pending command to 'sending' and return them, so
    a command is claimed by exactly one caller (no double-send).

    A command already in 'sending' whose claim is older than STALE_SENDING_SECONDS
    is treated as abandoned (the process that claimed it crashed mid-launch) and
    is reclaimed too, otherwise it would stay 'sending' forever, never retried
    (only 'pending' is normally claimed) and never pruned."""
    now = now if now is not None else time.time()
    claimed = []

    def mut(commands):
        for c in commands:
            status = c.get("status")
            due = float(c.get("send_ts", 0)) - command_prestage(c, lead) <= now
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




def has_all(units):
    """True when any unit count is the literal "all"."""
    return any(str(n).strip().lower() == "all" for n in (units or {}).values())


def resolve_all_units(units, home):
    """Replace every "all" with what `home` says is actually standing there.

    `home` is a {unit: count} reading of the village - the rally point's own
    numbers at send time, or the bot's last snapshot when this is only building
    a preview. Unit counts typed as numbers are left exactly as they are: the
    user asked for that many, and being short is the rally point's call to make.
    """
    home = home or {}
    out = {}
    for unit, count in (units or {}).items():
        if str(count).strip().lower() == "all":
            try:
                count = int(home.get(unit, 0) or 0)
            except (TypeError, ValueError):
                count = 0
        else:
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
        if count > 0:
            out[unit] = count
    return out


def fit_escort(escort, counts, followers):
    """Shrink the per-wave escort to what the village can actually spare.

    Only matters for a resolved-at-send-time train: the army that came home may
    be smaller than it was when the command was queued, and a train that lands
    with slightly thinner escorts still conquers - one that refuses to leave
    does not. Returns (escort, notes).
    """
    escort, notes = dict(escort or {}), []
    if followers <= 0:
        return {}, notes
    for unit, per_wave in sorted(escort.items()):
        have = counts.get(unit, 0)
        if per_wave * followers <= have:
            continue
        fits = have // followers
        notes.append("escort trimmed to %d %s per wave (only %d home)"
                     % (fits, unit, have))
        if fits > 0:
            escort[unit] = fits
        else:
            del escort[unit]
    return escort, notes


def split_train(units, mode="front", escort=None):
    """Split one stack into a noble train: N waves, exactly one noble each.

    A train exists because a village is only conquered when its loyalty is
    driven below zero, which takes several nobles, and because each noble must
    arrive in its own command. The waves land within a few hundred ms of each
    other, so the defender cannot snipe the gap between them.

    mode "front": the whole army rides with the first noble and every following
    noble takes only `escort` (the classic 25-50 light cavalry). Hits hardest,
    but a defender who snipes the first wave kills the stack.
    mode "even": every wave gets an equal share (1/N) of the army, remainders
    going to the first. Costs some punch, survives a snipe on any single wave.

    Returns (waves, error). `units` must be plain numbers by this point - an
    "all" entry is resolved against the rally point first (resolve_all_units),
    at schedule time for the preview and again just before sending.
    """
    units = dict(units or {})
    for unit, count in units.items():
        if str(count).strip().lower() == "all":
            return None, "'all' has to be resolved to a count before splitting"
    counts = {}
    for unit, count in units.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[unit] = count

    nobles = counts.get("snob", 0)
    if nobles < 2:
        return None, "a noble train needs at least 2 nobles"

    escort = {u: int(n) for u, n in (escort or {}).items()
              if str(n).strip().isdigit() and int(n) > 0 and u != "snob"}

    if mode == "even":
        waves = []
        for index in range(nobles):
            wave = {"snob": 1}
            for unit, count in counts.items():
                if unit == "snob":
                    continue
                share = count // nobles
                if index == 0:
                    share += count % nobles  # remainder rides with the first
                if share > 0:
                    wave[unit] = share
            waves.append(wave)
        return waves, None

    # Front-loaded: the followers take their escort out of the same stack, so
    # the escort has to actually be in it.
    followers = nobles - 1
    for unit, per_wave in escort.items():
        needed = per_wave * followers
        if counts.get(unit, 0) < needed:
            return None, ("not enough %s for the escort: %d in the stack, %d needed "
                          "(%d per wave x %d following nobles)"
                          % (unit, counts.get(unit, 0), needed, per_wave, followers))

    first = {}
    for unit, count in counts.items():
        if unit == "snob":
            first["snob"] = 1
            continue
        left = count - escort.get(unit, 0) * followers
        if left > 0:
            first[unit] = left
    waves = [first]
    for _ in range(followers):
        wave = {"snob": 1}
        wave.update(escort)
        waves.append(wave)
    return waves, None


def prepare_command(wrapper, origin_id, x, y, units, support=False):
    """Open the rally point and run the confirm step, but do NOT launch yet.

    Returns (confirm_data, server_duration, error). confirm_data is the ready-to
    -launch form; server_duration is TribalWars' own travel time for this command
    in seconds (the authoritative figure for hitting an exact arrival).
    With support=True the command is an "Ondersteunen" (support) send instead
    of an attack - used by the snipe engine.

    A unit count of "all" means: every one of that unit at home at SEND time,
    resolved from the rally point page itself - so the command still goes out
    when the village holds fewer troops than planned."""
    units = dict(units or {})
    want_all = [u for u in units if str(units[u]).strip().lower() == "all"]
    fixed = {}
    for u, n in units.items():
        if u in want_all:
            continue
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n > 0:
            fixed[u] = n
    if not want_all and not fixed:
        return None, 0, "no units selected"

    # 1) Open the rally point to collect the form's hidden fields + token.
    open_url = f"game.php?village={origin_id}&screen=place&target_type=coord"
    pre = wrapper.get_url(open_url)
    if not pre:
        return None, 0, "could not open rally point"
    if want_all:
        home = Extractor.units_in_place(pre)
        if not home:
            return None, 0, "could not read available units for 'all' selection"
        for u in want_all:
            n = home.get(u, 0)
            if n > 0:
                fixed[u] = n
        if not fixed:
            return None, 0, "no units at home for the 'all' selection"
    units = fixed
    pre_data = {k: v for k, v in Extractor.attack_form(pre)}
    pre_data.update({str(u): str(n) for u, n in units.items()})
    pre_data.update({"x": x, "y": y, "target_type": "coord"})
    # The submit button's name tells the server the command type.
    if support:
        pre_data["support"] = "Ondersteunen"
    else:
        pre_data["attack"] = "Aanvallen"

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
    if not conf:
        return None, 0, "rally point confirm request failed"
    if '<div class="error_box">' in conf.text:
        # Surface the game's own reason (e.g. newbie/points protection, invalid
        # target, no troops) - callers like the player-farm loop decide from it.
        box = re.search(r'<div class="error_box">\s*(.*?)\s*</div>',
                        conf.text, re.S)
        detail = re.sub(r"<[^>]+>", " ", box.group(1)).strip() if box else ""
        detail = re.sub(r"\s+", " ", detail)
        return None, 0, "rally point rejected the command%s" % (
            ": %s" % detail if detail else " (bad target or no troops)")

    duration = Extractor.attack_duration(conf)
    confirm_data = {}
    # The confirm form carries a field for the *other* command type too; strip
    # it so the launch unambiguously matches the button that was pressed.
    drop_key = "attack" if support else "support"
    for k, v in Extractor.attack_form(conf):
        if k == drop_key:
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
        command.get("target_y"), command.get("units") or {},
        support=bool(command.get("support")))
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
        msg = "%s sent (server travel %ds)" % (
            "support" if command.get("support") else "attack", duration)
    return ok, msg


def resolve_train_waves(wrapper, origin_id, units, spec, notes):
    """Split a train against the village's troops as they stand right now.

    Reads the rally point once (the same page prepare_command opens, so this is
    one extra request per train) and divides that. The number of waves is what
    was planned when the command was queued, capped by the nobles really there:
    the pre-stage window was sized for that many waves, and a wave whose noble
    is missing would only fail at the rally point anyway.

    Returns (waves, error) and appends any adjustment to `notes`.
    """
    page = wrapper.get_url(
        f"game.php?village={origin_id}&screen=place&target_type=coord")
    if not page:
        return None, "could not open the rally point to count troops"
    home = Extractor.units_in_place(page)
    if not home:
        return None, "could not read the troops at home"

    counts = resolve_all_units(units, home)
    planned = int(spec.get("nobles") or 0)
    budget = max(planned, int(spec.get("max_waves") or 0))
    available = counts.get("snob", 0)
    # A noble count typed as a number is a wish, not a fact - if that many are
    # not standing here, sending the wave anyway just earns a rejection from the
    # rally point. Cap on what the village actually holds.
    at_home = int(home.get("snob", 0) or 0)
    if at_home and available > at_home:
        available = at_home
    if available < 1:
        return None, "no nobles at home when the train was due"
    if budget and available > budget:
        notes.append("%d nobles home, sending %d waves (the window was "
                     "pre-staged for that many)" % (available, budget))
        available = budget
    elif planned and available < planned:
        notes.append("%d wave%s instead of %d (only %d noble%s home)"
                     % (available, "" if available == 1 else "s", planned,
                        available, "" if available == 1 else "s"))
    counts["snob"] = available
    if available < 2:
        notes.append("single noble left, sending it as one command")

    escort, escort_notes = fit_escort(
        spec.get("escort"), counts, available - 1)
    notes.extend(escort_notes)

    if available < 2:
        # split_train needs two nobles to be a train; one noble is just a
        # command, and the whole army rides with it either way.
        return [counts], None
    return split_train(counts, mode=spec.get("mode") or "front", escort=escort)


def execute_timed_train(wrapper, command, network_lead=NETWORK_LEAD):
    """Fire a noble train so the first wave LANDS at command['arrival_ts'].

    Every wave is prepared up front (open + confirm, during the pre-stage
    window), so when the launch moment comes only the bare launch requests are
    left and the waves leave back-to-back - the gap between them is one request
    round-trip, not a whole rally-point sequence.

    Preparing a wave does not spend troops, and each wave is a disjoint slice of
    what is home, so the confirms all pass and the launches still validate as
    the earlier waves consume their share. Returns (ok, message).
    """
    waves = command.get("waves") or []
    origin = command.get("origin_id")
    x, y = command.get("target_x"), command.get("target_y")
    notes = []

    spec = command.get("train") or {}
    if spec.get("dynamic"):
        # The command was queued with "all": read the village now and split what
        # is actually standing there, so troops that were out farming when this
        # was scheduled still ride along - and troops that never came back do
        # not make the whole train fail on a count that no longer exists.
        waves, err = resolve_train_waves(
            wrapper, origin, command.get("units") or {}, spec, notes)
        if err:
            return False, err

    prepared, failed = [], []
    for index, units in enumerate(waves):
        confirm_data, duration, err = prepare_command(wrapper, origin, x, y, units)
        if err:
            if index == 0:
                # The first wave carries the army (front-loaded) or an equal
                # share of it; without it there is no train worth sending.
                return False, "wave 1 could not be prepared: %s" % err
            failed.append("wave %d: %s" % (index + 1, err))
            continue
        prepared.append((index, confirm_data, duration))

    arrival = float(command.get("arrival_ts", 0))
    duration = prepared[0][2]
    if duration > 0 and arrival > 0:
        wait = (arrival - duration - network_lead) - time.time()
        if wait > 0:
            time.sleep(wait)
        elif wait < -2:
            logger.warning("Noble train %s launching %.1fs late",
                           command.get("id"), -wait)

    started = time.time()
    sent, errors = [], list(failed)
    for index, confirm_data, _duration in prepared:
        ok, msg = fire_command(wrapper, origin, confirm_data)
        if ok:
            sent.append(index + 1)
        else:
            errors.append("wave %d: %s" % (index + 1, msg))
    spread = int((time.time() - started) * 1000)

    if not sent:
        return False, "no wave left the village (%s)" % ("; ".join(errors) or "unknown")
    message = "train sent: %d/%d waves over %dms (server travel %ds)" % (
        len(sent), len(waves), spread, duration)
    for detail in notes + errors:
        message += " - %s" % detail
    return True, message


def run_due(wrapper, lead=PRESTAGE_SECONDS, network_lead=NETWORK_LEAD, path=None):
    """Claim and fire every command entering its pre-stage window. Each is claimed
    (pending -> sending) atomically so it fires exactly once, prepared, fired at
    the precise moment, then marked sent/failed. Returns the number sent.

    Claimed commands are handled soonest-first; note they fire sequentially, so
    two separately queued commands due within the same prestage window are not
    frame-accurate relative to each other. Waves that must leave together belong
    in one command's `waves` list, which is prepared up front and fired
    back-to-back (see execute_timed_train) - that is how a noble train is sent."""
    sent = 0
    claimed = sorted(claim_due(path=path, lead=lead),
                     key=lambda c: float(c.get("send_ts", 0)))
    for c in claimed:
        cid = c.get("id")
        try:
            if c.get("waves"):
                ok, msg = execute_timed_train(wrapper, c, network_lead=network_lead)
            else:
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
