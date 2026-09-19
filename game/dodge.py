"""
Auto-dodge (alpha): keep a village's troops alive through the attacks you tag.

Rename an incoming attack so its name carries the trigger text (default
"DODGE THIS" - "Ram >>>> DODGE THIS <<<<" works, the unit in front is only the
speed tag) and the bot takes everything standing in that village out just
before the hit and brings it back seconds after. Nothing to arm per attack: the
tag is the arming, done in game or from the dashboard.

This is not a snipe. There is no millisecond to hit; the troops only have to be
out while the tagged attacks land and home soon afterwards, so the troops are
still there to snipe with when the nobles come later that day.

How one dodge runs:

- Tagged attacks on the same village that land close together are one dodge:
  out before the first, back after the last. Whether two attacks are "close"
  is `merge_seconds` apart, and never more than one trip can cover.
- The troops leave `leave_before_seconds` before the first hit (they may leave
  a little earlier when many villages dodge at once - out early is harmless,
  still home at the hit is not), as support to the nearest village of your own
  that is far enough away and not under attack itself.
- The command is cancelled halfway, so the troops walk back in as long as they
  walked out and arrive `return_after_seconds` after the last hit. A cancelled
  command returns at S + 2k whole seconds (see game/csnipe.py for the live
  measurements); the cancel moment is planned with the c-snipe engine's own
  arithmetic, which never brings troops back early.
- A cancel that fails leaves the troops standing as support in your own
  village: alive, just not home. That is why the target is always one of your
  villages and never a barbarian one, where a failed cancel is a fight.

Limits the game sets:

- A command can only be cancelled within the world's command_cancel_time
  (usually 10 minutes) of sending, and the cancel is the halfway point, so one
  trip lasts at most about twice that. Tagged attacks spread over longer than
  that get separate dodges, and the troops come home in between.
- The destination has to be far enough away that the troops are still walking
  at the cancel; a nearer one would take the support before it can be pulled.

State lives in cache/dodges.json (attack_scheduler's locked, atomic queue file),
shared between the bot (plans and executes) and the dashboard (shows, cancels).
One runner thread interleaves every dodge on one timeline: many villages can be
under attack in the same minute, and one blocking dodge at a time would leave
most of them standing in the hit.
"""

import logging
import time

from core.filemanager import FileManager
from core.notification import Notification
from core.server_clock import GameClock
from game import attack_scheduler, csnipe
from game.incomings import (field_distance, incoming_tag_warning,
                            load_world_speeds, unit_travel_seconds)

DODGE_FILE = "cache/dodges.json"

DEFAULTS = {
    "enabled": False,
    "trigger": "DODGE THIS",
    "leave_before_seconds": 90,
    "return_after_seconds": 5,
    "merge_seconds": 300,
    # The second kind: dodge, but keep a small blocker home so fakes hit
    # something instead of an empty village.
    "keep_trigger": "DODGE KEEP",
    "keep_spear": 100,
    "keep_sword": 100,
    "keep_spy": 10,
}
KEEP_UNITS = ("spear", "sword", "spy")

# The send has to have happened this long before the hit, or it is too late to
# try: a send takes a few requests, and a dodge that leaves as the attack lands
# is worse than none (the confirm may still go through after the hit).
MIN_SEND_BEFORE_HIT_MS = 20_000
# Keep this much of the cancel window unused on each side of the halfway point.
# A cancel is never fired early, but it can fire late when many dodges are due
# at once, and a cancel that lands outside the window is refused by the game -
# the troops then arrive as support instead of coming home.
CANCEL_WINDOW_SLACK_MS = 90_000
# The destination must still be this far away at the cancel moment, so a cancel
# that fires late still catches the troops walking.
CANCEL_ARRIVAL_MARGIN_MS = 120_000
# Time the troops need at home between two dodges before they can leave again.
TURNAROUND_MS = 5_000
# Sends start this early by default, plus this much per other send due first:
# one send is ~4-8s of requests, and a queue of them must all be out in time.
SEND_EARLY_MS = 8_000
SEND_EARLY_PER_BACKLOG_MS = 6_000
MAX_SEND_ATTEMPTS = 3
RETRY_SPACING_MS = 6_000
REPLAN_SECONDS = 10
# The destinations tried per send before giving up on this attempt.
MAX_TARGET_TRIES = 3

ACTIVE = ("planned", "out")

logger = logging.getLogger("Dodge")


# -- settings -----------------------------------------------------------------

# Where each setting lives in config.json: the "defence" section, next to
# whatever else defends a village, with the dodge's keys prefixed.
CONFIG_SECTION = "defence"
CONFIG_KEYS = {
    "enabled": "dodge",
    "trigger": "dodge_trigger",
    "leave_before_seconds": "dodge_leave_before_seconds",
    "return_after_seconds": "dodge_return_after_seconds",
    "merge_seconds": "dodge_merge_seconds",
    "keep_trigger": "dodge_keep_trigger",
    "keep_spear": "dodge_keep_spear",
    "keep_sword": "dodge_keep_sword",
    "keep_spy": "dodge_keep_spy",
}


def settings_from(config):
    """The dodge settings from a config dict, with defaults filled in."""
    raw = (config or {}).get(CONFIG_SECTION) or {}
    out = dict(DEFAULTS)
    for key, config_key in CONFIG_KEYS.items():
        if raw.get(config_key) is not None:
            out[key] = raw[config_key]
    out["enabled"] = bool(out["enabled"])
    out["trigger"] = str(out["trigger"] or "").strip()
    out["keep_trigger"] = str(out["keep_trigger"] or "").strip()
    for key in ("leave_before_seconds", "return_after_seconds", "merge_seconds",
                "keep_spear", "keep_sword", "keep_spy"):
        try:
            out[key] = max(0, int(float(out[key])))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    # Leaving later than this is not a dodge any more, see MIN_SEND_BEFORE_HIT_MS.
    out["leave_before_seconds"] = max(out["leave_before_seconds"],
                                      MIN_SEND_BEFORE_HIT_MS // 1000 + 10)
    out["return_after_seconds"] = max(out["return_after_seconds"], 1)
    return out


def is_tagged(incoming, trigger):
    """True when the incoming's in-game name or dashboard tag has the trigger."""
    if not trigger:
        return False
    needle = trigger.lower()
    for field in ("game_label", "tag"):
        if needle in str(incoming.get(field) or "").lower():
            return True
    return False


def dodge_mode(incoming, settings):
    """Which dodge a name asks for: "full" (everything leaves), "keep" (a
    blocker stays for fakes) or None. A name matching both triggers takes the
    longer one - it is the more specific, and one trigger may well contain the
    other ("DODGE" and "DODGE KEEP")."""
    hits = [(len(settings.get(key) or ""), mode)
            for key, mode in (("trigger", "full"), ("keep_trigger", "keep"))
            if is_tagged(incoming, settings.get(key))]
    return max(hits)[1] if hits else None


def keep_units(settings):
    """The blocker a "keep" dodge leaves home, without the zeros."""
    return {u: settings["keep_" + u] for u in KEEP_UNITS if settings.get("keep_" + u)}


def max_trip_ms(cancel_window_ms):
    """Longest a single dodge can keep the troops out: the cancel is the
    halfway point and has to fall inside the cancel window, with slack."""
    return max(0, 2 * (cancel_window_ms - CANCEL_WINDOW_SLACK_MS))


# -- planning (pure) -------------------------------------------------------------

def _hit_ms(incoming):
    if incoming.get("arrival_ms"):
        return int(incoming["arrival_ms"])
    if incoming.get("arrival"):
        return int(incoming["arrival"]) * 1000
    return None


def plan(incomings, settings, now_ms, cancel_window_ms, busy_until=None,
         covered=None):
    """Work out the dodges the tagged incomings need. Pure: no I/O.

    incomings   - cached incoming commands (cache/incomings/*.json contents)
    busy_until  - {village_id: ms} troops of that village are out until then
                  (a dodge already under way); attacks landing before that
                  moment are already covered by it
    covered     - incoming ids an earlier dodge has already dealt with (sent,
                  finished, failed or cancelled by you) - never planned again

    Returns a list of dicts: village_id, incoming_ids, hits_ms, send_at_ms,
    return_ms, and status "planned" or "skipped" with a reason.
    """
    busy_until = busy_until or {}
    covered = set(covered or ())
    lead = settings["leave_before_seconds"] * 1000
    ret = settings["return_after_seconds"] * 1000
    trip_max = max_trip_ms(cancel_window_ms)
    split_gap = max(settings["merge_seconds"] * 1000,
                    lead + ret + TURNAROUND_MS + MIN_SEND_BEFORE_HIT_MS)

    by_village = {}
    for inc in incomings:
        mode = dodge_mode(inc, settings)
        if not mode:
            continue
        hit = _hit_ms(inc)
        vid = inc.get("target_id")
        cid = str(inc.get("command_id") or "")
        if not hit or not vid or not cid or cid in covered or hit <= now_ms:
            continue
        if hit <= busy_until.get(str(vid), 0):
            continue  # the troops are already out for this one
        by_village.setdefault(str(vid), []).append((hit, cid, mode))

    out = []
    for vid, hits in sorted(by_village.items()):
        hits.sort()
        clusters = [[hits[0]]]
        for hit, cid, mode in hits[1:]:
            cur = clusters[-1]
            first = cur[0][0]
            if hit - cur[-1][0] <= split_gap \
                    and hit + ret - (first - lead) <= trip_max:
                cur.append((hit, cid, mode))
            else:
                clusters.append([(hit, cid, mode)])
        busy = busy_until.get(vid, 0)
        for cluster in clusters:
            first, last = cluster[0][0], cluster[-1][0]
            send_at = max(first - lead, busy + TURNAROUND_MS if busy else 0)
            # A blocker only stays when every hit in the trip asked for one:
            # a "keep" hit sharing a trip with a full dodge's nuke would leave
            # the blocker standing in the nuke.
            keep = keep_units(settings) \
                if all(m == "keep" for _, _, m in cluster) else None
            entry = {
                "village_id": vid,
                "incoming_ids": [cid for _, cid, _ in cluster],
                "hits_ms": [hit for hit, _, _ in cluster],
                "send_at_ms": int(send_at),
                "return_ms": int(last + ret),
                "status": "planned",
                "keep": keep,
            }
            if send_at > first - MIN_SEND_BEFORE_HIT_MS:
                entry["status"] = "skipped"
                entry["result"] = (
                    "the troops are still out from the dodge before and only "
                    "get home %ds before this hit - too late to leave again"
                    % max(0, (first - busy) // 1000))
            elif last + ret - send_at > trip_max:
                # Only when the send was pushed later by a previous trip - the
                # cluster itself was built to fit.
                entry["status"] = "skipped"
                entry["result"] = "longer than one cancellable trip"
            else:
                busy = last + ret
            out.append(entry)
    return out


def needed_travel_ms(send_ms, return_ms):
    """How long the trip to the destination must take for the halfway cancel
    to still catch the troops walking."""
    return (return_ms - send_ms) // 2 + CANCEL_ARRIVAL_MARGIN_MS


def pick_destinations(village_id, locations, attacked, slowest_base, speeds,
                      need_ms):
    """Own villages to dodge to, best first: not under attack themselves (a
    failed cancel parks the troops there), nearest first, and far enough that
    the slowest unit going is still walking at the cancel. Villages under
    attack are only offered when nothing else qualifies."""
    here = locations.get(str(village_id))
    if not here:
        return []
    world_speed, unit_speed = speeds
    fits = []
    for vid, loc in locations.items():
        if vid == str(village_id):
            continue
        distance = field_distance(here, loc)
        travel = unit_travel_seconds(distance, slowest_base, world_speed,
                                     unit_speed) * 1000
        if travel < need_ms:
            continue
        fits.append((vid in attacked, distance, vid, loc))
    fits.sort()
    return [(vid, loc) for _, _, vid, loc in fits]


# -- storage --------------------------------------------------------------------

def _path(path=None):
    return path or FileManager.get_path(DODGE_FILE)


def load_dodges(path=None):
    return attack_scheduler.load_schedule(path=_path(path))


def _update(mutator, path=None):
    return attack_scheduler.update(mutator, path=_path(path))


def _patch(dodge_id, path=None, **fields):
    def mut(entries):
        for e in entries:
            if e.get("id") == dodge_id:
                e.update(fields)
    _update(mut, path)


def _get(dodge_id, path=None):
    for e in load_dodges(path):
        if e.get("id") == dodge_id:
            return e
    return None


def _event(dodge_id, message, path=None):
    logger.info("[%s] %s", dodge_id, message)
    stamp = time.strftime("%H:%M:%S")

    def mut(entries):
        for e in entries:
            if e.get("id") == dodge_id:
                events = e.setdefault("events", [])
                events.append("%s %s" % (stamp, message))
                del events[:-30]
    _update(mut, path)


def _finish(dodge_id, status, result, path=None, notify=True, **fields):
    _patch(dodge_id, path=path, status=status, result=result,
           finished=int(time.time()), **fields)
    _event(dodge_id, "%s: %s" % (status, result), path=path)
    if notify:
        entry = _get(dodge_id, path) or {}
        Notification.send("TWB dodge %s (village %s): %s"
                          % (status, entry.get("village_id", "?"), result),
                          category="attack")


def cancel(dodge_id, path=None):
    """The dashboard's cancel button. A planned dodge is dropped (and its
    attacks are not planned again); one that is out is brought home now -
    which may be before the hit, and the page says so."""
    def mut(entries):
        for e in entries:
            if e.get("id") != dodge_id:
                continue
            if e.get("status") == "planned":
                e["status"] = "cancelled"
                e["result"] = "cancelled from the dashboard before it left"
                e["finished"] = int(time.time())
                return "cancelled"
            if e.get("status") == "out":
                e["recall_now"] = True
                return "recall_requested"
        return None
    return _update(mut, path)


def prune(max_age=86400, path=None):
    now = int(time.time())

    def mut(entries):
        entries[:] = [
            e for e in entries
            if e.get("status") in ACTIVE
            or now - int(e.get("finished", e.get("created", now))) < max_age
        ]
    _update(mut, path)


# -- cache readers ------------------------------------------------------------------

def _load_incomings():
    out = []
    try:
        names = FileManager.list_directory("cache/incomings", ends_with=".json")
    except Exception:
        return out
    for name in names:
        entry = FileManager.load_json_file("cache/incomings/%s" % name)
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _managed():
    """{village_id: (location, troops)} for every managed village."""
    out = {}
    try:
        names = FileManager.list_directory("cache/managed", ends_with=".json")
    except Exception:
        return out
    for name in names:
        entry = FileManager.load_json_file("cache/managed/%s" % name) or {}
        loc = (entry.get("public") or {}).get("location")
        if isinstance(loc, list) and len(loc) == 2:
            out[name[:-len(".json")]] = (
                [int(loc[0]), int(loc[1])], entry.get("troops") or {})
    return out


# -- the runner -------------------------------------------------------------------

def replan(settings, now_ms, path=None):
    """Bring the stored plan in line with the tagged incomings as they stand.

    Dodges that already left are never touched. Planned ones are rebuilt from
    scratch each time - an attack can be tagged, untagged or recalled at any
    moment - keeping each one's id when its first attack is unchanged, so the
    dashboard's rows stay put."""
    incomings = _load_incomings()
    cancel_window = csnipe._cancel_window_ms()
    by_id = {str(i.get("command_id")): i for i in incomings}
    speeds = load_world_speeds()
    fresh_warnings = []   # (village, text) for dodges planned this pass

    def mut(entries):
        busy, covered, planned = {}, set(), {}
        for e in entries:
            status = e.get("status")
            if status == "planned":
                planned[(e["village_id"], e["incoming_ids"][0])] = e
                continue
            covered.update(e.get("incoming_ids") or [])
            if status == "out":
                vid = str(e["village_id"])
                busy[vid] = max(busy.get(vid, 0), int(e.get("return_ms", 0)))
        fresh = plan(incomings, settings, now_ms, cancel_window,
                     busy_until=busy, covered=covered)
        keep = [e for e in entries if e.get("status") != "planned"]
        for f in fresh:
            old = planned.get((f["village_id"], f["incoming_ids"][0]))
            # A name the flight time rules out - above all the "Ram" that can
            # only be a noble, where dodging hands the noble an empty village.
            # Not refused (it may be meant), but said, once, where it is seen.
            warnings = []
            for cid in f["incoming_ids"]:
                w = incoming_tag_warning(by_id.get(cid) or {}, speeds)
                if w:
                    warnings.append(w["text"])
            f["warnings"] = warnings
            if old:
                # Same dodge, possibly with an attack added or dropped: keep
                # its id, attempts and log, take the new timing.
                new_warnings = [w for w in warnings
                                if w not in (old.get("warnings") or [])]
                old.update(f)
                f = old
            else:
                f["id"] = "dg%x%s" % (int(time.time() * 1000) & 0xffffffff,
                                      f["incoming_ids"][0][-4:])
                f["created"] = int(time.time())
                new_warnings = warnings
            if f["status"] == "planned":
                fresh_warnings.extend((f["village_id"], w) for w in new_warnings)
            if f["status"] == "skipped":
                # A skip is final: recorded once, with its reason.
                f["finished"] = int(time.time())
                if not any(e.get("status") == "skipped"
                           and e.get("incoming_ids") == f["incoming_ids"]
                           for e in keep):
                    keep.append(f)
                    Notification.send(
                        "TWB dodge skipped (village %s): %s"
                        % (f["village_id"], f["result"]), category="attack")
                continue
            keep.append(f)
        entries[:] = keep
        return len([f for f in fresh if f["status"] == "planned"])

    # Recalled or untagged attacks simply drop out of `incomings`, and with
    # them the planned dodge that was waiting for them.
    planned = _update(mut, path)
    # Sent after the file lock is released: a slow Telegram call must not hold
    # up the dashboard.
    for vid, text in fresh_warnings:
        Notification.send("TWB dodge warning (village %s): the attack %s. "
                          "Dodging it lets it land on an empty village - "
                          "cancel the dodge on the Defense page if that is "
                          "not what you want." % (vid, text), category="attack")
    return planned


def _send(wrapper, clock, entry, path):
    """Take the village's troops out as support to one of our own villages.
    Returns True when the dodge is out (and its cancel planned)."""
    did = entry["id"]
    vid = str(entry["village_id"])
    managed = _managed()
    if vid not in managed:
        _finish(did, "failed", "the village is not in the bot's cache yet - "
                "let the bot run it once", path=path)
        return False
    here, troops = managed[vid]
    world_speed, unit_speed, base_speeds = load_world_speeds()
    # The slowest unit standing at home sets the pace. The snapshot can be a
    # few minutes old; the server's own travel time is checked again below.
    home_units = [u for u, n in troops.items()
                  if u in base_speeds and _int(n) > 0 and u != "militia"]
    slowest = max((base_speeds[u] for u in home_units),
                  default=base_speeds.get("spear", 18))
    now = clock.server_now_ms() if clock.offset_ms is not None else time.time() * 1000
    return_ms = int(entry["return_ms"])
    need = needed_travel_ms(max(now, entry["send_at_ms"] - SEND_EARLY_MS), return_ms)
    attacked = {str(i.get("target_id")) for i in _load_incomings()
                if (_hit_ms(i) or 0) > now}
    locations = {v: loc for v, (loc, _) in managed.items()}
    targets = pick_destinations(vid, locations, attacked, slowest,
                                (world_speed, unit_speed), need)
    if not targets:
        _event(did, "no village of yours is far enough away for the cancel "
               "to catch the troops walking (needs %d min at the slowest "
               "unit's pace)" % (need // 60000 + 1), path=path)
        return False

    units = {u: "all" for u in attack_scheduler.UNIT_KEYS}
    keep = entry.get("keep") or None
    for tvid, (tx, ty) in targets[:MAX_TARGET_TRIES]:
        confirm, duration, err = attack_scheduler.prepare_command(
            wrapper, vid, tx, ty, units, support=True, clock=clock, keep=keep)
        if err and "only scouts" in err:
            confirm, duration, err = attack_scheduler.prepare_command(
                wrapper, vid, tx, ty, {"spy": "all"}, support=True, clock=clock,
                keep=keep)
        if err and "no troops at home" in err:
            _finish(did, "done", "nothing at home to dodge with", path=path,
                    notify=False)
            return False
        if err and keep and "nothing the command asks for" in err:
            _finish(did, "done", "only the blocker is home - nothing left to "
                    "dodge with, it all stays", path=path, notify=False)
            return False
        if err:
            _event(did, "could not prepare the send to %s|%s: %s"
                   % (tx, ty, err), path=path)
            return False
        now = clock.server_now_ms()
        if duration * 1000 < needed_travel_ms(now, return_ms):
            _event(did, "%s|%s is only %ds away for the troops at home - "
                   "trying the next village" % (tx, ty, duration), path=path)
            continue
        latest = get_first_hit(entry) - MIN_SEND_BEFORE_HIT_MS
        if now > latest:
            _finish(did, "failed", "too late to leave before the hit - the "
                    "troops are still home", path=path)
            return False
        fired_at = clock.server_now_ms()
        ok, msg = attack_scheduler.fire_command(wrapper, vid, confirm,
                                                expect="support")
        if not ok:
            # The game may have made a command anyway; never leave the troops
            # walking without a planned cancel.
            _, _, stray = csnipe._locate_outgoing(
                wrapper, clock, vid, tx, ty, fired_at + duration * 1000)
            if stray:
                wrapper.get_url(stray)
            _event(did, "send refused: %s%s" % (
                msg, " (the command it made was recalled)" if stray else ""),
                path=path)
            return False
        command_id, arrival_ms, cancel_url = csnipe._locate_outgoing(
            wrapper, clock, vid, tx, ty, fired_at + duration * 1000)
        if arrival_ms is not None:
            send_low = send_high = arrival_ms - duration * 1000
        else:
            send_low = fired_at - 1000
            send_high = fired_at + int(clock.rtt * 1000) + 1000
        if not cancel_url:
            _finish(did, "failed", "sent, but no cancel link was found - the "
                    "troops will arrive as support in %s|%s and stay there "
                    "until you recall them" % (tx, ty), path=path,
                    target={"id": tvid, "x": tx, "y": ty})
            return False
        cancel_at, _, k = csnipe._plan_cancel(send_low, send_high, return_ms)
        _patch(did, path=path, status="out", send_ms=int(send_low),
               outgoing_id=command_id, cancel_url=cancel_url,
               cancel_at_ms=int(cancel_at),
               return_planned_ms=int(send_low + 2000 * k),
               travel_seconds=int(duration),
               target={"id": tvid, "x": tx, "y": ty})
        _event(did, "out: everything at home%s left as support to %s|%s "
               "(%d min walk) - cancel in %ds, back at %s"
               % (" except the blocker (%s)" % ", ".join(
                   "%d %s" % (n, u) for u, n in keep.items()) if keep else "",
                  tx, ty, duration // 60, (cancel_at - clock.server_now_ms()) // 1000,
                  _clock_text(send_low + 2000 * k)), path=path)
        return True
    _event(did, "none of the nearest villages is far enough away for the "
           "troops at home", path=path)
    return False


def _cancel(wrapper, clock, entry, path):
    did = entry["id"]
    recall = bool(entry.get("recall_now"))
    if not recall:
        clock.sleep_until(int(entry["cancel_at_ms"]), lead=False)
    res = wrapper.get_url(entry["cancel_url"])
    if res is None:
        res = wrapper.get_url(entry["cancel_url"])
    target = entry.get("target") or {}
    if res is None:
        return _finish(did, "failed", "the cancel request failed - the troops "
                       "will arrive as support in %s|%s and stay there until "
                       "you recall them" % (target.get("x"), target.get("y")),
                       path=path)
    send = int(entry.get("send_ms") or 0)
    cancelled = clock.server_now_ms()
    back = send + 2000 * max(0, int(cancelled - send) // 1000)
    # The cancel page is not proof (it renders a return the real event does
    # not follow - see csnipe); the returning command in the village's list is.
    confirmed = False
    if target.get("x") is not None:
        _, back_ms, _ = csnipe._locate_outgoing(
            wrapper, clock, entry["village_id"], target["x"], target["y"], back)
        if back_ms is not None:
            back, confirmed = back_ms, True
    if not confirmed:
        _event(did, "could not read the returning command back; the return "
               "time below is worked out, not read", path=path)
    if recall:
        return _finish(did, "done", "recalled from the dashboard - back at %s"
                       % _clock_text(back), path=path, notify=False,
                       return_actual_ms=int(back))
    late = back - int(entry["return_ms"])
    note = "" if late < 5000 else " (%ds later than planned - the runner was busy)" \
        % (late // 1000)
    _finish(did, "done", "cancelled - the troops are back at %s, %ds after "
            "the last hit%s" % (_clock_text(back),
                               (back - max(entry["hits_ms"])) // 1000, note),
            path=path, notify=False, return_actual_ms=int(back))


def get_first_hit(entry):
    return min(int(h) for h in entry.get("hits_ms") or [0])


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clock_text(ms):
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))


class Runner:
    """One timeline for every dodge on the account. tick() does at most one
    action and says how long it may sleep; the thread in twb.py loops it."""

    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.clock = GameClock()
        self.last_plan = 0.0
        self.last_prune = 0.0

    def now_ms(self):
        if self.clock.offset_ms is None:
            return time.time() * 1000
        return self.clock.server_now_ms()

    def tick(self, settings, path=None):
        now_wall = time.time()
        if now_wall - self.last_prune > 3600:
            prune(path=path)
            self.last_prune = now_wall
        if settings["enabled"] and now_wall - self.last_plan >= REPLAN_SECONDS:
            replan(settings, self.now_ms(), path=path)
            self.last_plan = now_wall
        entries = load_dodges(path)
        now = self.now_ms()

        # Cancels first when due: a dodge that is out has to come home no
        # matter what, and it is also the one thing that still runs with the
        # switch turned off.
        outs = [e for e in entries if e.get("status") == "out"]
        for e in sorted(outs, key=lambda e: e.get("cancel_at_ms", 0)):
            if e.get("recall_now") or e.get("cancel_at_ms", 0) <= now + 1500:
                try:
                    _cancel(self.wrapper, self.clock, e, path)
                except Exception as exc:
                    logger.exception("dodge %s cancel crashed", e.get("id"))
                    _finish(e["id"], "failed", "cancel crashed: %s" % exc,
                            path=path)
                return 0.2

        if not settings["enabled"]:
            # Switched off: nothing new leaves. Planned dodges are dropped so
            # the page does not promise what will not happen.
            if any(e.get("status") == "planned" for e in entries):
                def mut(items):
                    items[:] = [x for x in items if x.get("status") != "planned"]
                _update(mut, path)
            return 2.0

        planned = sorted((e for e in entries if e.get("status") == "planned"),
                         key=lambda e: e["send_at_ms"])
        for backlog, e in enumerate(planned):
            due = e["send_at_ms"] - SEND_EARLY_MS - SEND_EARLY_PER_BACKLOG_MS * backlog
            due = max(due, int(e.get("retry_at_ms") or 0))
            if due > now:
                continue
            attempts = int(e.get("attempts", 0)) + 1
            _patch(e["id"], path=path, attempts=attempts,
                   retry_at_ms=int(now + RETRY_SPACING_MS))
            try:
                if self.clock.offset_ms is None:
                    self.clock.sync(self.wrapper, "game.php?village=%s&screen=overview"
                                    % e["village_id"])
                sent = _send(self.wrapper, self.clock, e, path)
            except Exception as exc:
                logger.exception("dodge %s send crashed", e.get("id"))
                _event(e["id"], "send crashed: %s" % exc, path=path)
                sent = False
            if not sent:
                current = _get(e["id"], path) or {}
                if current.get("status") == "planned" and (
                        attempts >= MAX_SEND_ATTEMPTS
                        or self.now_ms() > get_first_hit(e) - MIN_SEND_BEFORE_HIT_MS):
                    _finish(e["id"], "failed", "could not get the troops out "
                            "(%d attempts) - they are still home for the hit"
                            % attempts, path=path)
            return 0.2

        # Nothing due: sleep until the next thing that is, at most 2s so new
        # tags and cancel requests from the dashboard are picked up.
        wake = [e["cancel_at_ms"] for e in outs] + [
            max(e["send_at_ms"] - SEND_EARLY_MS - SEND_EARLY_PER_BACKLOG_MS * i,
                int(e.get("retry_at_ms") or 0))
            for i, e in enumerate(planned)]
        if not wake:
            return 2.0
        return max(0.2, min(2.0, (min(wake) - 1500 - now) / 1000.0))
