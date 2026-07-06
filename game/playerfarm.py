"""
Curated player farming ("hit list").

The regular farm loop (game/attack.py) only trusts barbarian villages - it
cannot judge the liabilities of attacking a real player. This module farms
*player* villages the user has explicitly marked as cleared, each with its own
sending village, troop package (e.g. 35 light + 1 spy) and cadence (e.g.
hourly), and it stops itself the moment the reports say the player is back:

- red report (everything we sent died)            -> stop + notify
- any unit type wiped out                          -> stop + notify
- defenders visible in the report (player rebuilt) -> stop + notify
- yellow report where the loot does not clearly    -> stop + notify
  out-earn the resource value of the dead troops
  (loot must exceed PROFIT_FACTOR x loss cost)

A stopped target never resumes on its own - the user re-enables it from the
dashboard once they know the village is clear again (auto-resume would mean
repeatedly poking an active player).

Evaluation is driven by the report cache that the village loop already
maintains (game/reports.py): each cycle we look at reports for the target that
are newer than the last one we judged (eval_after), in order, and update the
target's totals and status. Sends go through the rally point with the exact
configured troops - not the Farm Assistant, whose templates are shared with
the barb loop.

The hit list lives in cache/player_farms.json, using attack_scheduler's
locked atomic file helpers so the dashboard (add/pause/remove) and the bot
(sends/status) never clobber each other.
"""

import datetime
import logging
import math
import random
import time

from core.filemanager import FileManager
from core.notification import Notification
from game import attack_scheduler
from game.simulator import Simulator

PLAYER_FARM_FILE = "cache/player_farms.json"

DEFAULT_INTERVAL_MIN = 60
# A fixed cadence ("exactly every 30 min") is a bot signature, so every run
# reschedules itself at interval +/- this fraction: 0.10 turns "every 100 min"
# into a fresh 90-110 min draw each time. Config: farms.player_farm_jitter.
INTERVAL_JITTER = 0.10
# A yellow report only passes when loot > PROFIT_FACTOR x the resource value
# (wood+clay+iron recruit cost) of the troops that died.
PROFIT_FACTOR = 2.0
# Like the barb loop's player-owned rule: don't hit real players at night.
NIGHT_SKIP_START, NIGHT_SKIP_END = 23, 8

logger = logging.getLogger("PlayerFarm")

# Recruit cost (total resources) per unit, for valuing losses.
UNIT_COST = {
    name: stats["wood"] + stats["clay"] + stats["iron"]
    for name, stats in Simulator.pool.items()
    if all(k in stats for k in ("wood", "clay", "iron"))
}


def _resolve(path):
    return path or FileManager.get_path(PLAYER_FARM_FILE)


def load_farms(path=None):
    """Current hit list (always a list)."""
    return attack_scheduler.load_schedule(path=_resolve(path))


def _update(mutator, path=None):
    return attack_scheduler.update(mutator, path=_resolve(path))


def add_farm(entry, path=None):
    """Append a new hit-list target and return it (with an assigned id)."""
    entry.setdefault("status", "active")
    entry.setdefault("eval_after", int(time.time()))
    entry.setdefault("totals", {"sends": 0, "loot": 0, "loss_cost": 0})
    return attack_scheduler.add_command(entry, path=_resolve(path))


def toggle_farm(farm_id, path=None):
    """Pause an active target, or (re-)activate a paused/stopped one. A manual
    resume clears the stop reason and skips past the report that caused it.
    Returns the new status, or None when the id is unknown."""
    def mut(commands):
        for c in commands:
            if c.get("id") != farm_id:
                continue
            if c.get("status") == "active":
                c["status"] = "paused"
            else:
                c["status"] = "active"
                c["stop_reason"] = None
                c["eval_after"] = int(time.time())
                c["reject_count"] = 0
            return c["status"]
        return None
    return _update(mut, path)


def remove_farm(farm_id, path=None):
    """Drop a target from the hit list. Returns True if one was removed."""
    def mut(commands):
        before = len(commands)
        commands[:] = [c for c in commands if c.get("id") != farm_id]
        return len(commands) < before
    return _update(mut, path)


def loss_cost(losses):
    """Resource value (recruit cost) of a {unit: dead_count} dict."""
    return sum(UNIT_COST.get(unit, 0) * int(count or 0)
               for unit, count in (losses or {}).items())


# -- target economy (production calculator) ----------------------------------

# Standard TW mine curve: level 1 = 30/h at world speed 1, x1.163118 per level;
# an unbuilt mine still trickles ~5/h.
MINE_FACTOR = 1.163118
LIGHT_CARRY_FALLBACK = 80


def mine_hourly(level, world_speed=1.0):
    """Hourly output of one mine at `level`."""
    level = int(level or 0)
    base = 5.0 if level <= 0 else 30.0 * MINE_FACTOR ** (level - 1)
    return base * world_speed


def warehouse_capacity(level):
    """Standard TW warehouse capacity for a storage level (caps at 400k)."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 1000
    if not level:
        return 1000
    return min(400000, int(round(1000 * (1.2294934 ** (level - 1)))))


def estimate_economy(buildings, resources=None, scouted_at=None,
                     world_speed=1.0, now=None):
    """Judge a target's economy from scout intel: hourly production per
    resource (mine levels x the mine curve x world speed) and the stock
    estimated to be sitting there right now - the scouted amount plus
    everything produced since, capped at the warehouse."""
    buildings = buildings or {}
    hourly = {res: mine_hourly(buildings.get(res), world_speed)
              for res in ("wood", "stone", "iron")}
    capacity = warehouse_capacity(buildings.get("storage"))
    now = now if now is not None else time.time()
    hours_ago = max(0.0, (now - int(scouted_at)) / 3600.0) if scouted_at else None
    est_now = None
    if resources is not None and hours_ago is not None:
        est_now = {
            res: int(min(capacity, int(resources.get(res) or 0)
                         + hourly[res] * hours_ago))
            for res in hourly
        }
    return {
        "hourly": {res: int(v) for res, v in hourly.items()},
        "hourly_total": int(sum(hourly.values())),
        "capacity": capacity,
        "est_now": est_now,
        "est_now_total": sum(est_now.values()) if est_now else None,
        "scouted_hours_ago": round(hours_ago, 1) if hours_ago is not None else None,
    }


def suggest_light(hourly_total, interval_min, carry=LIGHT_CARRY_FALLBACK):
    """Light cavalry needed to carry one farm interval's worth of production."""
    per_run = float(hourly_total) * float(interval_min) / 60.0
    return max(1, math.ceil(per_run / float(carry or LIGHT_CARRY_FALLBACK)))


def classify_report(entry):
    """Judge one of our attack/scout reports on a hit-list target.

    Returns (verdict, detail): verdict is 'ok' or 'stop'. Uses the parsed
    report structure from game/reports.py: extra.units_sent / units_losses are
    ours, extra.defence_units is what the fight (or spy) revealed."""
    extra = entry.get("extra") or {}
    losses = entry.get("losses") or {}
    sent = extra.get("units_sent") or {}
    defenders = {u: int(n) for u, n in (extra.get("defence_units") or {}).items()
                 if int(n or 0) > 0}
    loot = sum(int(v or 0) for v in (extra.get("loot") or {}).values())

    if defenders:
        return "stop", "defenders seen: %s" % (
            ", ".join("%s %d" % (u, n) for u, n in defenders.items()))
    if sent and all(int(losses.get(u, 0)) >= int(n) for u, n in sent.items()):
        return "stop", "red report - the whole farm run died"
    wiped = [u for u, n in sent.items()
             if int(n) > 0 and int(losses.get(u, 0)) >= int(n)]
    if wiped:
        return "stop", "unit type(s) wiped out: %s" % ", ".join(wiped)
    if losses:
        cost = loss_cost(losses)
        if loot < PROFIT_FACTOR * cost:
            return "stop", ("unprofitable: lost %d res worth of troops for "
                            "%d res loot" % (cost, loot))
    return "ok", None


class PlayerFarmManager:
    """Account-level runner: evaluates fresh reports for every hit-list target
    and fires the due farm runs. Called once per main-loop cycle on the main
    (human-paced) wrapper."""

    def __init__(self, wrapper=None, config=None, path=None):
        self.wrapper = wrapper
        self.config = config or {}
        self.path = path

    # -- report evaluation ---------------------------------------------------

    def evaluate(self, farm, reports):
        """Judge every not-yet-seen report on this target, oldest first.

        Only our own attack/scout reports from the farm's sending village are
        considered. Totals and last_report are updated as we go; the first
        'stop' verdict freezes the target (later reports stay unjudged until a
        manual resume moves eval_after past them)."""
        relevant = []
        for report_id, entry in reports.items():
            if not entry or entry.get("dest") != str(farm.get("target_id")):
                continue
            if entry.get("origin") and str(entry["origin"]) != str(farm.get("source_id")):
                continue
            when = (entry.get("extra") or {}).get("when")
            if not when or int(when) <= int(farm.get("eval_after") or 0):
                continue
            relevant.append((int(when), report_id, entry))

        stop_reason = None
        updates = {}
        totals = dict(farm.get("totals") or {"sends": 0, "loot": 0, "loss_cost": 0})
        for when, report_id, entry in sorted(relevant):
            verdict, detail = classify_report(entry)
            extra = entry.get("extra") or {}
            loot = sum(int(v or 0) for v in (extra.get("loot") or {}).values())
            cost = loss_cost(entry.get("losses"))
            totals["loot"] = int(totals.get("loot") or 0) + loot
            totals["loss_cost"] = int(totals.get("loss_cost") or 0) + cost
            updates["last_report"] = {
                "id": report_id,
                "when": when,
                "verdict": verdict,
                "detail": detail,
                "losses": entry.get("losses") or {},
                "loot": loot,
                "loss_cost": cost,
                "resources_left": extra.get("resources"),
            }
            updates["eval_after"] = when
            if verdict == "stop":
                stop_reason = detail
                break

        if not updates:
            return
        updates["totals"] = totals
        if stop_reason:
            updates["status"] = "stopped"
            updates["stop_reason"] = stop_reason
            logger.warning("Player farm %s (%s) auto-stopped: %s",
                           farm.get("id"), farm.get("target_name"), stop_reason)
            Notification.send(
                "TWB player farm stopped: %s (%s|%s) - %s" % (
                    farm.get("target_name") or "?", farm.get("target_x"),
                    farm.get("target_y"), stop_reason))
        farm.update(updates)

        def mut(commands):
            for c in commands:
                if c.get("id") == farm.get("id"):
                    c.update(updates)
        _update(mut, self.path)

    # -- sending -------------------------------------------------------------

    def _night_window(self):
        if not (self.config.get("farms") or {}).get("player_farm_night_skip", True):
            return False
        hour = time.localtime().tm_hour
        return hour >= NIGHT_SKIP_START or hour < NIGHT_SKIP_END

    def _troops_at_home(self, village_id):
        managed = FileManager.load_json_file("cache/managed/%s.json" % village_id)
        return (managed or {}).get("available_troops") or {}

    def _forced_peace_conflict(self, duration):
        """True when the run would land inside a configured forced-peace window
        (same naive local-time parsing as the rest of the bot)."""
        windows = (self.config.get("farms") or {}).get("forced_peace_times") or []
        arrival = datetime.datetime.now() + datetime.timedelta(seconds=duration)
        for pair in windows:
            try:
                start = datetime.datetime.strptime(pair["start"], "%d.%m.%y %H:%M:%S")
                end = datetime.datetime.strptime(pair["end"], "%d.%m.%y %H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            if start <= arrival <= end:
                return True
        return False

    def send(self, farm):
        """Fire one farm run through the rally point with the exact configured
        troops. Returns (sent, hard_error): hard_error carries the game's own
        rejection text when the rally point refused the command outright -
        that is how a points/newbie-protection block ("grey on the map")
        surfaces, since the game gives us no advance signal for it."""
        units = {u: int(n) for u, n in (farm.get("units") or {}).items()
                 if int(n or 0) > 0}
        if not units:
            return False, None
        home = self._troops_at_home(farm.get("source_id"))
        lacking = [u for u, n in units.items() if int(home.get(u, 0) or 0) < n]
        if lacking:
            logger.info("Player farm %s: not enough %s at home in %s, skipping "
                        "this cycle", farm.get("id"), ",".join(lacking),
                        farm.get("source_id"))
            return False, None

        confirm_data, duration, err = attack_scheduler.prepare_command(
            self.wrapper, farm.get("source_id"), farm.get("target_x"),
            farm.get("target_y"), units)
        if err:
            logger.warning("Player farm %s: %s", farm.get("id"), err)
            # A rejection despite troops being at home is (almost always) a
            # persistent block - protection ratio, vacation mode, deleted
            # village. Transient network errors return different messages.
            return False, err if err.startswith("rally point rejected") else None
        if self._forced_peace_conflict(duration):
            logger.info("Player farm %s: would land inside forced peace, "
                        "skipping", farm.get("id"))
            return False, None
        ok, _msg = attack_scheduler.fire_command(
            self.wrapper, farm.get("source_id"), confirm_data)
        if ok:
            logger.info("Player farm run %s -> %s (%s|%s) with %s",
                        farm.get("source_id"), farm.get("target_name"),
                        farm.get("target_x"), farm.get("target_y"), units)
        return ok, None

    def run(self):
        """One pass: evaluate fresh reports, then send every due farm run."""
        farms = load_farms(self.path)
        if not any(f.get("status") == "active" for f in farms):
            return 0
        # The report cache is world-aware via FileManager, same as the bot's
        # own ReportManager writes it.
        reports = {}
        try:
            for name in FileManager.list_directory("cache/reports",
                                                   ends_with=".json"):
                reports[name[:-5]] = FileManager.load_json_file(
                    "cache/reports/%s" % name)
        except FileNotFoundError:
            pass

        sent = 0
        night = self._night_window()
        now = int(time.time())
        jitter = float((self.config.get("farms") or {}).get(
            "player_farm_jitter", INTERVAL_JITTER))
        for farm in farms:
            if farm.get("status") != "active":
                continue
            self.evaluate(farm, reports)
            if farm.get("status") != "active":
                continue
            interval = int(farm.get("interval_min") or DEFAULT_INTERVAL_MIN) * 60
            # next_due carries the jittered schedule; entries from before the
            # jitter existed (or never-sent ones) fall back to the plain math.
            due = farm.get("next_due")
            if due is None:
                last = int(farm.get("last_sent") or 0)
                due = last + interval if last else now
            if int(due) > now:
                continue
            if night:
                logger.debug("Player farm %s: night window, holding fire",
                             farm.get("id"))
                continue
            ok, hard_error = self.send(farm)
            if ok:
                sent += 1
                next_due = now + int(interval * random.uniform(1 - jitter,
                                                               1 + jitter))

                def mut(commands):
                    for c in commands:
                        if c.get("id") == farm.get("id"):
                            c["last_sent"] = now
                            c["next_due"] = next_due
                            c["reject_count"] = 0
                            totals = c.setdefault(
                                "totals", {"sends": 0, "loot": 0, "loss_cost": 0})
                            totals["sends"] = int(totals.get("sends") or 0) + 1
                _update(mut, self.path)
            elif hard_error:
                self._register_rejection(farm, hard_error)
        return sent

    # Stop after this many rally-point rejections in a row: one can be a race
    # (troops left between our cache check and the confirm), two consecutive
    # cycles with troops at home means the game is refusing the target - e.g.
    # the points/newbie protection kicked in and the village went grey.
    REJECT_STOP_AFTER = 2

    def _register_rejection(self, farm, error):
        count = int(farm.get("reject_count") or 0) + 1
        stop = count >= self.REJECT_STOP_AFTER
        updates = {"reject_count": count}
        if stop:
            updates["status"] = "stopped"
            updates["stop_reason"] = error
            logger.warning("Player farm %s (%s) auto-stopped after %d "
                           "rejections: %s", farm.get("id"),
                           farm.get("target_name"), count, error)
            Notification.send(
                "TWB player farm stopped: %s (%s|%s) - the game refuses the "
                "attack (%s). Points protection?" % (
                    farm.get("target_name") or "?", farm.get("target_x"),
                    farm.get("target_y"), error))
        farm.update(updates)

        def mut(commands):
            for c in commands:
                if c.get("id") == farm.get("id"):
                    c.update(updates)
        _update(mut, self.path)
