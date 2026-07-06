"""
Support-snipe engine.

A snipe lands defensive support in (or just before) the gap of an incoming
attack train: pick an incoming on the Defense tab's Snipe tab, choose one or
more village/pace options (each village offers one option per distinct unit
speed among its defensive troops at home), and the bot fires the support so it
is *processed* at the chosen millisecond - land_ms = first hit + a signed
offset (default lands just before the hit).

Timing works like the c-snipe engine, not the scheduled-attack runner: the
server clock is synced to ~half the round-trip time (csnipe._Clock), the rally
point confirm supplies the authoritative travel duration, and the final launch
request is slept-until to the millisecond. After the send the outgoing command
is read back so the achieved arrival (and its delta vs the target) is reported.

Troops can have left between arming and the send, so each snipe carries a
shortfall policy, applied against the rally point's live troop counts:
  scale  - send what is home, but abort under min_pct% of the planned total
  all    - always send whatever is home
  strict - abort on any shortfall

Storage reuses the c-snipe queue helpers (locked, atomic, cross-process file)
on a separate file (cache/snipes.json) shared between the bot process
(executes) and the web dashboard (arms/cancels). Snipes run sequentially on
their own thread, soonest send first; sends less than ~15s apart may make the
later one miss its window (it fails "too late" rather than landing off-target).
"""

import logging
import time

from core.extractors import Extractor
from core.filemanager import FileManager
from core.notification import Notification
from game import attack_scheduler, csnipe

SNIPE_FILE = "cache/snipes.json"

# Claim a snipe this long before its estimated send moment: enough for the
# clock sync + troop check + rally point prepare, short enough that the
# confirm token stays fresh.
PRESTAGE_SECONDS = 90
DEFAULT_MIN_PCT = 80
DEFAULT_OFFSET_MS = -100

logger = logging.getLogger("Snipe")


# -- queue storage: the c-snipe helpers on our own file -----------------------

def _path(path=None):
    return path or FileManager.get_path(SNIPE_FILE)


def load_snipes(path=None):
    """Current snipe queue (always a list)."""
    return csnipe.load_snipes(path=_path(path))


def arm(entry, path=None):
    """Append a new armed snipe and return it (with an assigned id)."""
    return csnipe.arm(entry, path=_path(path))


def disarm(snipe_id, path=None):
    """Disarm a snipe; before the send it is simply dropped, during the final
    wait the runner aborts the launch (the troops never leave)."""
    return csnipe.disarm(snipe_id, path=_path(path))


def prune(max_age_done=86400, path=None):
    return csnipe.prune(max_age_done=max_age_done, path=_path(path))


def next_start_ts(path=None):
    return csnipe.next_start_ts(path=_path(path))


def claim_due(path=None, now=None):
    return csnipe.claim_due(path=_path(path), now=now)


def _finish(snipe_id, status, result, path=None, notify=True, **fields):
    csnipe._patch(snipe_id, path=_path(path), status=status, result=result,
                  finished=int(time.time()), **fields)
    csnipe._event(snipe_id, "%s: %s" % (status, result), path=_path(path))
    if notify:
        Notification.send("TWB snipe %s: %s" % (status, result))


def _event(snipe_id, message, path=None):
    csnipe._event(snipe_id, message, path=_path(path))


# -- execution -----------------------------------------------------------------

def _apply_shortfall(planned, available, policy, min_pct):
    """Clamp the planned units to what is actually home right now.

    Returns (units_to_send, error). error is set when the policy says the
    snipe must be aborted instead of sent thinner than planned."""
    to_send = {}
    short = []
    for unit, count in planned.items():
        have = int(available.get(unit, 0))
        use = min(count, have)
        if use < count:
            short.append("%s %d/%d" % (unit, have, count))
        if use > 0:
            to_send[unit] = use
    if not to_send:
        return None, "no planned troops are home anymore"
    if not short:
        return to_send, None
    if policy == "strict":
        return None, "troops short (%s) and the policy is strict" % ", ".join(short)
    if policy == "all":
        return to_send, None
    ratio = 100.0 * sum(to_send.values()) / max(1, sum(planned.values()))
    if ratio < min_pct:
        return None, ("only %d%% of the planned troops are home (%s), under "
                      "the %d%% minimum" % (ratio, ", ".join(short), min_pct))
    return to_send, None


def execute(wrapper, snipe, path=None, network_lead=0.0):
    """Run one claimed snipe: verify troops, prepare, fire at the exact ms.

    land_ms is the target *processing* moment of the arrival; the launch is
    aimed at land_ms - server_travel so the support walks in on the chosen
    millisecond."""
    sid = snipe.get("id")
    village_id = snipe.get("village_id")
    land_ms = int(snipe.get("land_ms", 0))
    qpath = _path(path)

    # The units page carries both the live troop counts and the game state for
    # the clock sync, so one request covers both.
    clock = csnipe._Clock()
    res = clock.sync(wrapper, "game.php?village=%s&screen=place&mode=units"
                              "&display=units" % village_id)
    if res is None or clock.offset_ms is None:
        return _finish(sid, "failed", "could not sync the server clock "
                       "(session dead?)", path=path)
    _event(sid, "clock synced (offset %+dms, rtt %dms)"
           % (clock.offset_ms, clock.rtt * 1000), path=path)

    if land_ms - clock.server_now_ms() < 5000:
        return _finish(sid, "failed", "claimed too late - the landing moment "
                       "is under 5s away", path=path)

    available = {}
    for unit, count in Extractor.units_in_village(res):
        try:
            available[unit] = int(count)
        except (TypeError, ValueError):
            continue
    planned = {u: int(n) for u, n in (snipe.get("units") or {}).items()
               if int(n or 0) > 0}
    to_send, err = _apply_shortfall(
        planned, available, snipe.get("shortfall") or "scale",
        int(snipe.get("min_pct") or DEFAULT_MIN_PCT))
    if err:
        return _finish(sid, "failed", err, path=path)
    if to_send != planned:
        _event(sid, "shortfall policy: sending %s (planned %s)"
               % (to_send, planned), path=path)

    confirm_data, duration, err = attack_scheduler.prepare_command(
        wrapper, village_id, snipe.get("target_x"), snipe.get("target_y"),
        to_send, support=True)
    if err:
        return _finish(sid, "failed", err, path=path)

    send_at = land_ms - duration * 1000
    now = clock.server_now_ms()
    if send_at < now + 1000:
        return _finish(sid, "failed", "too late: server travel is %ds so the "
                       "send moment already passed (%.1fs ago)"
                       % (duration, (now - send_at) / 1000.0), path=path)

    csnipe._patch(sid, path=qpath, send_ms=int(send_at),
                  travel_seconds=int(duration), units_sent=to_send)
    _event(sid, "sending in %.1fs (server travel %ds, aimed at .%03d)"
           % ((send_at - now) / 1000.0, duration, send_at % 1000), path=path)

    # Wait out the gap polling for a dashboard cancel; aborting here means the
    # troops simply stay home.
    if csnipe._wait_checking_disarm(sid, clock, send_at - 2000, qpath):
        return _finish(sid, "disarmed", "cancelled before the send - the "
                       "troops stayed home", path=path, notify=False)

    clock.sleep_until(send_at, network_lead)
    ok, _msg = attack_scheduler.fire_command(wrapper, village_id, confirm_data)
    if not ok:
        return _finish(sid, "failed", "launch request failed - support did "
                       "NOT leave", path=path)

    # Read the outgoing command back for the achieved arrival millisecond.
    command_id, arrival_ms, _cancel = csnipe._locate_outgoing(
        wrapper, clock, village_id, snipe.get("target_x"),
        snipe.get("target_y"), land_ms)
    if arrival_ms is not None:
        delta = arrival_ms - land_ms
        _finish(sid, "done", "support lands at .%03d, %+dms vs target"
                % (arrival_ms % 1000, delta), path=path,
                outgoing_id=command_id, arrival_actual_ms=int(arrival_ms),
                delta_ms=int(delta))
    else:
        _finish(sid, "done", "support sent (server travel %ds); could not "
                "read the ms arrival back" % duration, path=path,
                outgoing_id=command_id)


def run_due(wrapper, path=None, network_lead=0.0):
    """Claim and execute every due snipe, soonest send first. Returns the count."""
    executed = 0
    for snipe in sorted(claim_due(path=path),
                        key=lambda s: float(s.get("send_est_ts", s.get("start_ts", 0)))):
        try:
            execute(wrapper, snipe, path=path, network_lead=network_lead)
        except Exception as exc:  # never let one bad snipe kill the thread
            logger.exception("snipe %s crashed", snipe.get("id"))
            _finish(snipe.get("id"), "failed", "exception: %s" % exc, path=path)
        executed += 1
    return executed
