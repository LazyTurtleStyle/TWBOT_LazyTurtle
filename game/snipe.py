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
from game.incomings import load_label_endpoint, rename_command_ingame

SNIPE_FILE = "cache/snipes.json"

# Claim a snipe this long before its estimated send moment: enough for the
# clock sync + troop check + rally point prepare, short enough that the
# confirm token stays fresh.
PRESTAGE_SECONDS = 90
DEFAULT_MIN_PCT = 80
DEFAULT_OFFSET_MS = -100

# A claim is made against the dashboard's *estimated* send moment, and that
# estimate is only as good as the unit speeds it knows about. A paladin paces
# its whole command at paladin speed however slow the rest of it is: on
# 2026-09-21 a spear+archer+paladin support came back from the rally point at
# 10 min/field instead of spear's 18, putting the real send 55 minutes past the
# estimate. The estimator knows that rule now, but this stays as the backstop
# for whatever it does not know yet. Waiting a gap out inside execute() is
# wrong twice over - the
# runner is serial, so the snipe holds the thread and every send moment inside
# the gap is lost (8 of 65 that day), and the confirm token being held goes
# stale long before the launch uses it. Past this much slack the snipe goes
# back in the queue carrying the server's own send moment, to be re-claimed
# with a fresh token at the proper time. The cost is one extra rally-point
# prepare; the alternative is losing the rest of the queue.
REQUEUE_AFTER_SECONDS = 300
# An estimate that never settles would otherwise re-open the rally point on
# every pass, so a snipe only gets so many corrections.
MAX_REQUEUES = 2

# Where landings are remembered for the self-calibrating lead. Measured on
# nl116 on 2026-10-05: with the fixed sched_lead_seconds the same send landed
# -31ms at 13:12 and +28ms at 16:47, drifting with the evening server load, so
# no fixed number held for more than an hour. Each read-back landing is stored
# as the delta it would have had with no lead at all ("raw"), and the next send
# fires early (or late) by the median of the most recent ones.
CALIBRATION_FILE = "cache/snipe_calibration.json"
CALIBRATION_SAMPLES = 15
CALIBRATION_MAX_AGE = 3 * 3600
# A reading this far off is a broken read-back, not latency.
CALIBRATION_CAP_MS = 80

# Name a kept snipe's own support command after how close it lands, e.g.
# "581|430 [6 ms]" = lands 6ms before the attack it was aimed at, so the
# rally point shows at a glance which supports are in place (the Toxic Donut
# millisecond tagger did the same; its "Sort ms" button only reads "[N ms]"
# with the space). Empty string turns it off.
DEFAULT_RENAME_FORMAT = "{x}|{y} [{ms} ms]"


def wrong_side_of_hit(arrival_ms, land_ms, hit_ms):
    """Why a landing ended up on the wrong side of the hit, or None. Pure.

    The hit is a wall: the support has to land on the same side of it as the
    aim did. Aimed before the hit, a landing strictly after it defended nothing
    however close it looks - support that arrives 2ms after the nuke is just a
    stack standing in a smoking village. Aimed after the hit on purpose (a
    positive offset, e.g. behind a clearing nuke), a landing strictly before it
    is the miss.

    Landing on the hit's own millisecond is NOT counted here - see
    coin_flip_on_hit. hit_ms None (a snipe armed before the hit was recorded,
    whose incoming is no longer cached) means the question cannot be answered,
    which is not the same as passing."""
    if hit_ms is None:
        return None
    delta = arrival_ms - land_ms
    after = arrival_ms - hit_ms
    if land_ms <= hit_ms:
        if after > 0:
            return ("landed %+dms vs the aim - %dms AFTER the hit it was "
                    "meant to beat" % (delta, after))
    elif after < 0:
        return ("landed %+dms vs the aim - %dms BEFORE the hit it was "
                "meant to follow" % (delta, -after))
    return None


def coin_flip_on_hit(arrival_ms, hit_ms):
    """Whether the support landed on the attack's own millisecond. Pure.

    Which of two commands sharing a millisecond resolves first is not ours to
    know, so this is a 50/50 on whether the support defended anything. It is
    never recalled: half a chance of holding the village beats certainly
    walking the troops home. It is still worth saying out loud, because a
    landing like this reads as a +1ms bullseye against the aim."""
    return hit_ms is not None and arrival_ms == hit_ms


def keep_verdict(arrival_ms, land_ms, hit_ms, limit_ms):
    """Whether a fired snipe is kept, and if not, why. Pure.

    The limit is measured from the aim (land_ms); the side of the hit is
    checked separately, see wrong_side_of_hit.

    limit_ms 0 still means nothing is ever recalled - that is what it has
    always meant and troops walking home unasked would be a nasty surprise -
    but it no longer means nothing is *checked*. It used to return before the
    hit-side test ran, so on 2026-09-21 three snipes armed without a tolerance
    landed on or after the nuke and were all reported "done ... +107ms vs
    target", which reads like a success. With no limit set a wrong-side
    landing now comes back kept-but-not-a-hit, for the caller to report
    honestly.

    A landing on the attack's own millisecond is always kept, whatever the
    limit says, because it is a coin flip rather than a miss - but it comes
    back with a reason so it is not filed as the +1ms bullseye it resembles.

    Returns (keep, reason). reason is set whenever something is wrong with the
    landing, including when it is kept anyway."""
    delta = arrival_ms - land_ms
    if coin_flip_on_hit(arrival_ms, hit_ms):
        return True, ("landed %+dms vs the aim, ON the attack's own "
                      "millisecond - a coin flip which resolves first" % delta)
    side = wrong_side_of_hit(arrival_ms, land_ms, hit_ms)
    if not limit_ms or limit_ms <= 0:
        return True, side
    if abs(delta) > limit_ms:
        return False, "landed %+dms, outside the %dms limit" % (delta, limit_ms)
    if side:
        return False, side
    return True, None


def _hit_of(snipe):
    """The attack's landing ms the snipe was armed against: stored at arming
    since this change, otherwise read from the incoming it was armed from."""
    if snipe.get("hit_ms"):
        return int(snipe["hit_ms"])
    inc = FileManager.load_json_file(
        "cache/incomings/%s.json" % snipe.get("incoming_id")) \
        if snipe.get("incoming_id") else None
    if isinstance(inc, dict) and inc.get("arrival_ms"):
        return int(inc["arrival_ms"])
    return None

logger = logging.getLogger("Snipe")


# -- queue storage: the c-snipe helpers on our own file -----------------------

def calibration_lead_ms(samples, now, network_lead=0.0):
    """Extra lead in ms (positive fires earlier) on top of network_lead. Pure.

    The total lead becomes the median raw delta of the recent landings, so a
    run that has been landing +12ms late fires 12ms early. Returns 0 with no
    recent readings, which leaves the configured lead alone."""
    recent = [s for s in samples or []
              if now - s.get("when", 0) <= CALIBRATION_MAX_AGE
              and abs(s.get("raw_ms", 0)) <= CALIBRATION_CAP_MS]
    recent = sorted(recent, key=lambda s: s["when"])[-CALIBRATION_SAMPLES:]
    if not recent:
        return 0.0
    raws = sorted(s["raw_ms"] for s in recent)
    mid = len(raws) // 2
    median = raws[mid] if len(raws) % 2 else (raws[mid - 1] + raws[mid]) / 2.0
    return median - network_lead * 1000.0


def _load_calibration():
    data = FileManager.load_json_file(CALIBRATION_FILE) or {}
    return data.get("samples") or []


def _record_calibration(raw_ms, now):
    samples = _load_calibration() + [{"when": int(now), "raw_ms": int(raw_ms)}]
    FileManager.save_json_file_atomic({"samples": samples[-50:]},
                                      CALIBRATION_FILE)


def _drop_idle_connections(wrapper):
    """Make the clock sync open a fresh connection, like the launch will.

    The launch fires ~90s after the sync, on a connection the server has long
    closed, so it pays the reconnect. A sync that rode the previous snipe's
    still-open connection measured ~70-115ms round trips against ~160-215ms
    cold, and its sends landed +55..+81ms late (nl116, 2026-10-05, once
    snipes were queued 25s apart)."""
    web = getattr(wrapper, "web", None)
    for adapter in list((getattr(web, "adapters", None) or {}).values()):
        try:
            adapter.close()
        except Exception:
            pass


def kept_label(fmt, snipe, arrival_ms, hit_ms):
    """The new name for a kept support command, or None. Pure."""
    if not fmt or hit_ms is None or arrival_ms is None:
        return None
    try:
        return fmt.format(x=int(snipe.get("target_x")), y=int(snipe.get("target_y")),
                          ms=int(hit_ms) - int(arrival_ms))
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _rename_kept(wrapper, sid, command_id, label, path=None):
    """Rename our own outgoing support; never raises."""
    cfg = dict(load_label_endpoint() or {})
    if not cfg.get("screen") or not command_id or not label:
        return
    # The captured endpoint is the incoming-attack one; our own commands take
    # the same request with the "own" action.
    cfg["params"] = {"ajaxaction": "edit_own_comment", "id": "__ID__"}
    web = getattr(wrapper, "web", None)
    cookies = {c.name: c.value for c in web.cookies} if web is not None else {}
    headers = getattr(wrapper, "headers", None) or {}
    try:
        res = rename_command_ingame(command_id, label, cookies,
                                    getattr(wrapper, "endpoint", "") or "",
                                    headers.get("User-Agent"), label_cfg=cfg)
    except Exception as exc:  # a missing name is cosmetic, never fatal
        res = {"ok": False, "error": str(exc)}
    _event(sid, "renamed the support to %s" % label if res.get("ok")
           else "could not rename the support (%s)" % (res.get("error")
                                                       or res.get("reason")),
           path=path)


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
        Notification.send("TWB snipe %s: %s" % (status, result), category="attack")


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


def execute(wrapper, snipe, path=None, network_lead=0.0, presend_sync=False,
            rename_format=DEFAULT_RENAME_FORMAT):
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
    _drop_idle_connections(wrapper)
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

    # Far enough out that holding the runner (and the token) costs more than
    # coming back for it - see REQUEUE_AFTER_SECONDS.
    requeues = int(snipe.get("requeues") or 0)
    if send_at - now > REQUEUE_AFTER_SECONDS * 1000 and requeues < MAX_REQUEUES:
        current = csnipe._get(sid, qpath)
        # A disarm that landed while the rally point was being prepared would
        # be thrown away by flipping the status back to armed, so it wins.
        if current is None or current.get("disarm_requested"):
            return _finish(sid, "disarmed", "cancelled before the send - the "
                           "troops stayed home", path=path, notify=False)
        # start_ts/send_est_ts are local epoch seconds (claim_due compares them
        # against time.time()), while send_at is on the server clock.
        send_local = (send_at - clock.offset_ms) / 1000.0
        csnipe._patch(sid, path=qpath, status="armed", send_ms=int(send_at),
                      travel_seconds=int(duration),
                      send_est_ts=int(send_local),
                      start_ts=int(send_local - PRESTAGE_SECONDS),
                      requeues=requeues + 1)
        _event(sid, "server travel is %ds, %.1f min past the estimate - "
                    "requeued for %s rather than holding the runner"
               % (duration, (send_at - now) / 60000.0,
                  time.strftime("%H:%M:%S", time.localtime(send_local))),
               path=path)
        return None

    csnipe._patch(sid, path=qpath, send_ms=int(send_at),
                  travel_seconds=int(duration), units_sent=to_send)
    _event(sid, "sending in %.1fs (server travel %ds, aimed at .%03d)"
           % ((send_at - now) / 1000.0, duration, send_at % 1000), path=path)

    # Wait out the gap polling for a dashboard cancel; aborting here means the
    # troops simply stay home.
    if csnipe._wait_checking_disarm(sid, clock, send_at - 2000, qpath):
        return _finish(sid, "disarmed", "cancelled before the send - the "
                       "troops stayed home", path=path, notify=False)

    correction = calibration_lead_ms(_load_calibration(), time.time(),
                                     network_lead)
    if correction:
        _event(sid, "calibrated lead %+.0fms (median of recent landings)"
               % (network_lead * 1000 + correction), path=path)
    # Optionally measure the clock again ~2s before the launch, on a fresh
    # connection, instead of trusting the reading from ~90s ago.
    if snipe.get("presend_sync", presend_sync):
        _drop_idle_connections(wrapper)
        before = clock.offset_ms
        if clock.sync(wrapper, "game.php?village=%s&screen=place&mode=units"
                               "&display=units" % village_id) is not None:
            _event(sid, "re-synced before the launch (offset %+dms, was %+dms, "
                   "rtt %dms)" % (clock.offset_ms, before, clock.rtt * 1000),
                   path=path)
    # Same reason as the sync: fire on a fresh connection, whatever the gap
    # since the last request. A launch 25s after its sync rode the still-open
    # connection and landed -81ms (nl116, 2026-10-05).
    _drop_idle_connections(wrapper)
    clock.sleep_until(send_at, network_lead + correction / 1000.0)
    ok, msg = attack_scheduler.fire_command(wrapper, village_id, confirm_data,
                                            expect="support")
    if not ok:
        # Pass the game's own words through. A generic "launch request failed"
        # is the difference between knowing the token went stale and spending
        # an evening guessing.
        return _finish(sid, "failed", "support did NOT leave: %s" % msg,
                       path=path)

    # Read the outgoing command back for the achieved arrival millisecond.
    command_id, arrival_ms, cancel_url = csnipe._locate_outgoing(
        wrapper, clock, village_id, snipe.get("target_x"),
        snipe.get("target_y"), land_ms)
    if arrival_ms is not None:
        delta = arrival_ms - land_ms
        try:
            _record_calibration(delta + network_lead * 1000 + correction,
                                time.time())
        except Exception:  # calibration must never cost the recall below
            logger.debug("could not store the snipe calibration reading")
        # A snipe that misses the gap is not a smaller success, it is a stack
        # standing in a village it was never meant to garrison - and worse, it
        # reads as defence that is not where you think it is. With a tolerance
        # set, a command outside it is pulled straight back: the cancel window
        # is minutes wide and we are inside it by seconds, so this is the one
        # moment it can be taken back cleanly.
        #
        # This is what makes arming several worthwhile. Fire five, keep the
        # ones that land inside the limit, and the rest walk home.
        limit = snipe.get("max_delta_ms")
        try:
            limit = int(limit) if limit not in (None, "") else 0
        except (TypeError, ValueError):
            limit = 0
        hit_ms = _hit_of(snipe)
        keep, why = keep_verdict(arrival_ms, land_ms, hit_ms, limit)
        if not keep:
            recalled = False
            if cancel_url:
                recalled = wrapper.get_url(cancel_url) is not None
            if recalled:
                return _finish(sid, "missed", "%s - recalled" % why, path=path,
                               outgoing_id=command_id,
                               arrival_actual_ms=int(arrival_ms),
                               delta_ms=int(delta))
            return _finish(sid, "done",
                           "%s, but it could NOT be recalled - the troops are "
                           "on their way" % why, path=path,
                           outgoing_id=command_id,
                           arrival_actual_ms=int(arrival_ms),
                           delta_ms=int(delta))
        label = kept_label(rename_format, snipe, arrival_ms, hit_ms)
        if label and command_id:
            _rename_kept(wrapper, sid, command_id, label, path=path)
        if why:
            # Kept, but not a clean hit: either it shares the attack's
            # millisecond (a coin flip, always kept) or no tolerance was set so
            # nothing was ever going to be recalled. Either way it must not be
            # filed as the bullseye its delta makes it look like.
            note = ("" if coin_flip_on_hit(arrival_ms, hit_ms)
                    else " - kept (no tolerance set, so it was not recalled)")
            _finish(sid, "done", "%s%s" % (why, note), path=path,
                    outgoing_id=command_id, arrival_actual_ms=int(arrival_ms),
                    delta_ms=int(delta), flagged=True)
        else:
            _finish(sid, "done", "support lands at .%03d, %+dms vs target"
                    % (arrival_ms % 1000, delta), path=path,
                    outgoing_id=command_id, arrival_actual_ms=int(arrival_ms),
                    delta_ms=int(delta))
    else:
        _finish(sid, "done", "support sent (server travel %ds); could not "
                "read the ms arrival back" % duration, path=path,
                outgoing_id=command_id)


def run_due(wrapper, path=None, network_lead=0.0, presend_sync=False,
            rename_format=DEFAULT_RENAME_FORMAT):
    """Claim and execute every due snipe, soonest send first. Returns the count."""
    executed = 0
    for snipe in sorted(claim_due(path=path),
                        key=lambda s: float(s.get("send_est_ts", s.get("start_ts", 0)))):
        try:
            execute(wrapper, snipe, path=path, network_lead=network_lead,
                    presend_sync=presend_sync, rename_format=rename_format)
        except Exception as exc:  # never let one bad snipe kill the thread
            logger.exception("snipe %s crashed", snipe.get("id"))
            _finish(snipe.get("id"), "failed", "exception: %s" % exc, path=path)
        executed += 1
    return executed
