"""
Auto-noble barbarian villages (alpha).

Each job walks one barb's loyalty ("toestemming") down with noble attacks from
a fixed sending village until it is conquered, without ever overshooting:

- Loyalty is tracked from our own attack reports (the "Toestemming: Gedaald
  van X naar Y" line only nobles produce, extracted by game/reports.py into
  extra["loyalty"]). No report on the target yet -> assume 100. Loyalty
  regenerates over time, so the estimate adds +1/hour x world speed since the
  last report.
- The overshoot guard assumes every noble hits for the MAXIMUM drop (35): at
  estimated loyalty L at most ceil(L / 35) nobles may be under way at once.
  That is why a fresh 100-loyalty barb gets a 3-noble train first and the
  remaining 1-2 nobles only after the reports confirm what actually landed -
  sending a full 4-5 train blind risks a lucky roll conquering early and the
  trailing noble hitting a village that is already yours.
- With fewer nobles home than the guard allows, it sends what is there and
  repeats after they return (the "walk it down" pattern).
- Multiple nobles are sent as separate sequential attacks a few seconds apart
  (one noble per attack - extra nobles in a single attack add no loyalty
  drop). The in-game train/multi-send feature is not driven yet; the first
  confirm page containing a noble is dumped to cache/world/noble_confirm.html
  so support can be built from what the server actually renders.

Auto-stop (never auto-resume, matching the player-farm philosophy):
- target owner is no longer barb: another player took it -> stopped;
  we took it (village appears in our managed cache) -> done.
- a report shows our whole send died (red) or the noble died without its
  loyalty hit landing -> stopped.
- the rally point rejects the send twice in a row -> stopped.
Jobs are created disarmed and must be armed manually from the dashboard.

State lives in cache/noble_jobs.json using attack_scheduler's locked atomic
file helpers, shared safely between the bot and the web dashboard.
"""

import datetime
import logging
import math
import time

from core.extractors import Extractor
from core.filemanager import FileManager
from core.notification import Notification
from game import attack_scheduler

NOBLE_JOBS_FILE = "cache/noble_jobs.json"
# What the last command-overview read saw, for the dashboard. The webmanager
# has no game session of its own, so without this it can only show the bot's
# own sends and a train launched by hand reads as missing.
FLYING_FILE = "cache/noble_flying.json"

# A noble's loyalty hit is 20-35. The overshoot guard uses the max so a train
# can never contain a noble that a string of lucky rolls would waste on a
# village that is already ours; the "how many are still needed" display uses
# the min so the user sees the worst-case remaining count.
LOYALTY_DROP_MIN = 20
LOYALTY_DROP_MAX = 35
LOYALTY_START = 100

# Rally-point rejections in a row before the job stops itself (the first one
# can be a troop-count race; the second means the game refuses the command).
REJECT_STOP_AFTER = 2

# Extra slack (seconds) on top of the 2x travel time before in-flight nobles
# are considered home again (report ingest + cache refresh lag).
RETURN_MARGIN = 300

# The command overview is account-wide, so one request answers for every job.
# It is cached briefly to share that request between the escort reservation
# pass and the noble pass; the noble pass forces a refresh, because a stale
# hit there is exactly the mistake this whole check exists to prevent.
COMMANDS_CACHE_SECONDS = 120

logger = logging.getLogger("NobleBarb")


def _resolve(path):
    return path or FileManager.get_path(NOBLE_JOBS_FILE)


def load_jobs(path=None):
    """Current job list (always a list)."""
    return attack_scheduler.load_schedule(path=_resolve(path))


def _update(mutator, path=None):
    return attack_scheduler.update(mutator, path=_resolve(path))


def latest_loyalty_report(target_id):
    """Newest (when, loyalty) already on record for this village, or None.

    Reports are the only place loyalty is ever observed, and a barb is very
    often softened by hand before a job is made for it."""
    best = None
    try:
        names = FileManager.list_directory("cache/reports", ends_with=".json")
    except FileNotFoundError:
        return None
    for name in names:
        entry = FileManager.load_json_file("cache/reports/%s" % name)
        if not entry or str(entry.get("dest")) != str(target_id):
            continue
        extra = entry.get("extra") or {}
        loyalty = extra.get("loyalty")
        when = extra.get("when")
        if not when or not loyalty or len(loyalty) != 2:
            continue
        try:
            when, value = int(when), int(loyalty[1])
        except (TypeError, ValueError):
            continue
        if best is None or when > best[0]:
            best = (when, value)
    return best


def add_job(entry, path=None):
    """Append a new noble job and return it (with an assigned id).

    Jobs start paused - the user arms them explicitly from the dashboard."""
    entry.setdefault("status", "paused")
    entry.setdefault("eval_after", int(time.time()))
    # evaluate() only ever looks at reports newer than eval_after, so without
    # this a barb already nobled by hand starts the job assuming loyalty 100.
    # That is the dangerous direction to be wrong in: an over-estimate lets
    # the overshoot guard put more nobles in the air than is actually safe.
    if entry.get("loyalty") is None and entry.get("target_id"):
        seen = latest_loyalty_report(entry["target_id"])
        if seen:
            entry["loyalty"] = {"value": seen[1], "at": seen[0]}
            entry["eval_after"] = seen[0]
            logger.info("New job on %s starts at loyalty %d, seen in a report "
                        "from before it was created", entry["target_id"], seen[1])
    entry.setdefault("escort_min_pct", 80)
    entry.setdefault("totals", {"sends": 0, "nobles": 0})
    entry.setdefault("log", [])
    return attack_scheduler.add_command(entry, path=_resolve(path))


def toggle_job(job_id, path=None):
    """Arm a paused job, or pause an armed/stopped one. Arming a stopped job
    clears the stop reason (a conscious user decision, like player farms).
    A finished (done) job stays done. Returns the new status or None."""
    def mut(commands):
        for c in commands:
            if c.get("id") != job_id:
                continue
            if c.get("status") == "done":
                return "done"
            if c.get("status") == "armed":
                c["status"] = "paused"
            else:
                c["status"] = "armed"
                c["stop_reason"] = None
                c["reject_count"] = 0
            return c["status"]
        return None
    return _update(mut, path)


def move_job(job_id, direction, path=None):
    """Move a job one place up or down the list.

    The list order IS the priority order - see focus_budgets(). Returns True
    when the job actually moved (False at either end)."""
    step = -1 if str(direction) == "up" else 1

    def mut(commands):
        for index, c in enumerate(commands):
            if c.get("id") != job_id:
                continue
            swap = index + step
            if swap < 0 or swap >= len(commands):
                return False
            commands[index], commands[swap] = commands[swap], commands[index]
            return True
        return False
    return _update(mut, path)


def remove_job(job_id, path=None):
    """Drop a job. Returns True if one was removed."""
    def mut(commands):
        before = len(commands)
        commands[:] = [c for c in commands if c.get("id") != job_id]
        return len(commands) < before
    return _update(mut, path)


def world_speed():
    """The world's speed factor (loyalty regenerates +1/hour x speed)."""
    config = FileManager.load_json_file("cache/world/config.json") or {}
    try:
        return float(config.get("speed") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def estimate_loyalty(job, now=None, speed=None):
    """Current best loyalty estimate: last reported value + regeneration
    since, capped at 100. Without any report: 100 (the user's rule)."""
    now = now if now is not None else time.time()
    known = job.get("loyalty") or {}
    value = known.get("value")
    seen = known.get("at")
    if value is None:
        return LOYALTY_START
    speed = speed if speed is not None else world_speed()
    regen = max(0.0, (now - int(seen or now)) / 3600.0) * speed
    return min(LOYALTY_START, int(value) + int(regen))


def max_safe_nobles(loyalty):
    """How many nobles may be under way at once without any chance of one
    landing on a village that earlier maximum-luck hits already conquered."""
    if loyalty <= 0:
        return 0
    return max(1, math.ceil(loyalty / float(LOYALTY_DROP_MAX)))


def nobles_needed_worst_case(loyalty):
    """Upper bound on the nobles still required (every hit rolls minimum)."""
    if loyalty <= 0:
        return 0
    return math.ceil(loyalty / float(LOYALTY_DROP_MIN))


def troops_at_home(village_id, wrapper=None):
    """The troops standing in the village right now.

    With a wrapper this reads the rally point, which is what the game will
    actually let us send. The managed cache is only a snapshot written at the
    END of that village's run - after its own farm pass has sent the light
    cavalry out - so an escort decision made from it sees the leftovers rather
    than what is home when the noble pass runs, and a light cavalry escort can
    sit "2 short" forever while the village really has plenty. The snapshot is
    still the fallback when the page cannot be read."""
    if wrapper is not None:
        live = None
        try:
            res = wrapper.get_url(
                "game.php?village=%s&screen=place&target_type=coord" % village_id)
            live = Extractor.units_in_place(res) if res else None
        except Exception as exc:
            logger.warning("Could not read the rally point of village %s: %s",
                           village_id, exc)
        if live:
            return live
        logger.info("No live troop counts for village %s, falling back to the "
                    "snapshot from its last run", village_id)
    managed = FileManager.load_json_file("cache/managed/%s.json" % village_id)
    return (managed or {}).get("available_troops") or {}


_COMMANDS_CACHE = {"at": 0.0, "flying": None}


def nobles_in_flight(wrapper=None, village_id=None, now=None, force=False):
    """Every noble currently on its way somewhere, as {(x, y): count}, read
    from the account's own command overview.

    This is what lets the overshoot guard see attacks sent BY HAND. A job's
    in_flight bookkeeping only knows about the bot's own sends, so a manual
    noble train that lands between two cycles is invisible to it: the bot then
    sends the next noble into a village that is already ours and kills it.

    Returns None when the overview cannot be read. Callers must treat that as
    "do not send" - guessing zero here is precisely how nobles get wasted.
    """
    now = now if now is not None else time.time()
    if (not force and _COMMANDS_CACHE["flying"] is not None
            and now - _COMMANDS_CACHE["at"] < COMMANDS_CACHE_SECONDS):
        return _COMMANDS_CACHE["flying"]
    if wrapper is None or not village_id:
        return None
    commands = None
    try:
        # page=-1 lifts the 25-row pagination: without it only the soonest
        # arrivals are listed and a noble further out reads as absent.
        res = wrapper.get_url(
            "game.php?village=%s&screen=overview_villages&mode=commands"
            "&type=attack&page=-1" % village_id)
        if res:
            commands = Extractor.outgoing_commands(res)
    except Exception as exc:
        logger.warning("Could not read the command overview: %s", exc)
    if commands is None:
        return None
    flying = {}
    for command in commands:
        count = int((command.get("units") or {}).get("snob", 0) or 0)
        if count:
            key = (command["x"], command["y"])
            flying[key] = flying.get(key, 0) + count
    _COMMANDS_CACHE.update({"at": now, "flying": flying})
    try:
        FileManager.save_json_file(
            {"at": int(now),
             "targets": {"%s|%s" % key: count for key, count in flying.items()}},
            FLYING_FILE)
    except Exception as exc:  # a display aid must never break the send path
        logger.debug("Could not cache the in-flight nobles: %s", exc)
    return flying


def nobles_heading_to(job, flying):
    """How many nobles are already on their way to this job's target."""
    if not flying:
        return 0
    try:
        key = (int(job.get("target_x")), int(job.get("target_y")))
    except (TypeError, ValueError):
        return 0
    return int(flying.get(key, 0))


def escort_packages(job, home, nobles):
    """Check the escort trigger and build the per-attack escort packages.

    The configured escort is PER NOBLE. A send goes ahead when every escort
    unit has at least escort_min_pct% of the total (package x nobles) at home;
    each attack then carries its even share of what is actually available
    (capped at the configured package).

    Returns (packages, None) or (None, reason)."""
    escort = {u: int(n) for u, n in (job.get("escort") or {}).items()
              if int(n or 0) > 0}
    pct = max(0, min(100, int(job.get("escort_min_pct") or 0)))
    sendable = {}
    for unit, per_noble in escort.items():
        want_total = per_noble * nobles
        have = int(home.get(unit, 0) or 0)
        if have < math.ceil(want_total * pct / 100.0):
            return None, "%s: %d home, need at least %d%% of %d" % (
                unit, have, pct, want_total)
        sendable[unit] = min(have, want_total)
    packages = []
    for i in range(nobles):
        pack = {"snob": 1}
        for unit, total in sendable.items():
            share = total // nobles
            if i < total % nobles:
                share += 1
            if share > 0:
                pack[unit] = share
        packages.append(pack)
    return packages, None


def planned_send(job, now=None, wrapper=None, home=None, flying_map=None,
                 budget=None):
    """What this job would send if the noble pass ran right now.
    Returns (nobles, packages) or (0, None).

    This is the same decision NobleBarbManager.step() makes, minus the parts
    that need this cycle's reports (loyalty may still drop before the real
    send, which can only make the plan smaller) and minus the ownership stops
    (a target that flipped owner reserves troops for one last cycle). `home`
    is the sending village's troops; without it they are read through
    troops_at_home(wrapper). `flying_map` is nobles_in_flight() output, so a
    job the guard will refuse does not hold an escort hostage; without it the
    reservation stays optimistic, which costs a cycle of scavenging at worst."""
    if job.get("status") != "armed":
        return 0, None
    now = now if now is not None else time.time()
    in_flight = job.get("in_flight") or {}
    flying = int(in_flight.get("nobles") or 0)
    if flying and now < int(in_flight.get("back_at") or 0):
        return 0, None  # the train is still out; nothing to hold back
    flying = max(flying, nobles_heading_to(job, flying_map))
    allowed = max_safe_nobles(estimate_loyalty(job, now=now)) - flying
    if allowed <= 0:
        return 0, None
    if home is None:
        home = troops_at_home(job.get("source_id"), wrapper=wrapper)
    nobles_home = int(home.get("snob", 0) or 0)
    if nobles_home <= 0:
        return 0, None
    nobles = min(allowed, nobles_home)
    if budget is not None:
        nobles = min(nobles, budget)
    if nobles <= 0:
        return 0, None
    packages, _lacking = escort_packages(job, home, nobles)
    if packages is None:
        return 0, None
    return nobles, packages


def focus_budgets(jobs, flying_map=None, home_by_source=None, now=None,
                  speed=None):
    """How many nobles each armed job may send this cycle, as {job_id: count}.

    Without this every armed job simply took whatever the overshoot guard
    allowed, so a second barb started being walked down the moment the first
    one hit its 3-noble ceiling. Nobles ended up spread over several targets,
    all of them half-done.

    The list order is the priority order. Walking it from the top, each job
    claims every noble it could still need in the WORST case - every hit
    rolling the 20 minimum, so ceil(loyalty / 20) - less whatever is already
    flying at it. Only what survives that claim reaches the next job.

    Worst case is deliberate. The guard (ceil(loyalty / 35), assuming maximum
    luck) decides how many may be in the air at once; this decides how many
    are *spoken for*. Reserving on the optimistic number would hand a noble to
    the next target and then find the leader still needs it.

    So a leader at 100 loyalty with 3 already flying claims 5 - 3 = 2 more. It
    cannot send them (the guard is saturated), but they stay home for its next
    wave instead of going to another barb. A 4th and 5th noble at home would
    be genuinely spare and pass on down the list.
    """
    now = now if now is not None else time.time()
    speed = speed if speed is not None else world_speed()
    home_by_source = home_by_source or {}
    available = {}
    for source, home in home_by_source.items():
        available[str(source)] = int((home or {}).get("snob", 0) or 0)

    budgets = {}
    for job in jobs:
        if job.get("status") != "armed":
            continue
        source = str(job.get("source_id"))
        pool = available.get(source, 0)
        if pool <= 0:
            budgets[job.get("id")] = 0
            continue
        loyalty = estimate_loyalty(job, now=now, speed=speed)
        # Same "whoever sent them" reading step() uses: the job's own
        # bookkeeping misses trains launched by hand.
        in_flight = max(int((job.get("in_flight") or {}).get("nobles") or 0),
                        nobles_heading_to(job, flying_map))
        claim = min(pool, max(0, nobles_needed_worst_case(loyalty) - in_flight))
        allowed = max(0, max_safe_nobles(loyalty) - in_flight)
        budgets[job.get("id")] = min(allowed, claim)
        available[source] = pool - claim
    return budgets


def escort_reservations(jobs=None, path=None, now=None, wrapper=None,
                        focus=True):
    """Troops the armed noble jobs will claim at the end of this cycle, as
    {source_id: {unit: count}}.

    The noble pass runs last (see TWB.run_noble_barbs) so that it decides on
    fresh troop and report caches; without a reservation the barb shaper,
    scavenging and the player farms have already spent the escort by then and
    the job waits another cycle. Only jobs whose escort trigger is ALREADY
    satisfied reserve anything - a job still waiting for troops to be built
    never holds scavenging hostage.

    With a wrapper the sending villages are read live (one rally point request
    per village, however many jobs share it). That is what makes the whole
    thing work for a light cavalry escort: the cached snapshot is written
    after the farm pass has already sent the cavalry out, so it would show the
    escort as short in exactly the cycles where it is not."""
    reserve = {}
    home_by_village = {}
    jobs = jobs if jobs is not None else load_jobs(path)
    armed = [j for j in jobs if j.get("status") == "armed"]
    flying_map = nobles_in_flight(
        wrapper=wrapper, village_id=armed[0].get("source_id") if armed else None,
        now=now)
    for job in jobs:
        source = str(job.get("source_id"))
        if job.get("status") == "armed" and source not in home_by_village:
            home_by_village[source] = troops_at_home(source, wrapper=wrapper)
    # A job the focus rule will not let send must not hold an escort hostage
    # either, so the same priority pass runs here.
    budgets = focus_budgets(jobs, flying_map=flying_map,
                            home_by_source=home_by_village,
                            now=now) if focus else {}
    for job in jobs:
        source = str(job.get("source_id"))
        try:
            _nobles, packages = planned_send(
                job, now=now, home=home_by_village.get(source),
                flying_map=flying_map, budget=budgets.get(job.get("id")))
        except (TypeError, ValueError) as exc:
            logger.warning("Noble job %s: cannot plan a reservation: %s",
                           job.get("id"), exc)
            continue
        if not packages:
            continue
        bucket = reserve.setdefault(str(job.get("source_id")), {})
        for pack in packages:
            for unit, count in pack.items():
                if unit == "snob":
                    continue  # nothing else in the bot spends nobles
                bucket[unit] = bucket.get(unit, 0) + int(count)
    return reserve


class NobleBarbManager:
    """Account-level runner: called once per main-loop cycle on the main
    (human-paced) wrapper, after the village loop has refreshed troop and
    report caches."""

    def __init__(self, wrapper=None, config=None, path=None):
        self.wrapper = wrapper
        self.config = config or {}
        self.path = path

    # -- shared helpers --------------------------------------------------------

    def _log(self, job, message):
        """Append to the job's rolling event log (kept short) + process log."""
        logger.info("Noble job %s: %s", job.get("id"), message)
        entry = {"at": int(time.time()), "msg": message}

        def mut(commands):
            for c in commands:
                if c.get("id") == job.get("id"):
                    log = c.setdefault("log", [])
                    log.append(entry)
                    del log[:-20]
        _update(mut, self.path)
        job.setdefault("log", []).append(entry)

    def _save(self, job, updates):
        job.update(updates)

        def mut(commands):
            for c in commands:
                if c.get("id") == job.get("id"):
                    c.update(updates)
        _update(mut, self.path)

    def _waiting(self, job, reason):
        """Say why an armed job is idle. The reason goes to the process log
        every cycle but into the job's own log only when it changes: that log
        is a 20-entry ring, and repeating the same line every few minutes
        pushes the real events (sends, loyalty drops) out of it."""
        if job.get("waiting_reason") == reason:
            logger.info("Noble job %s: %s", job.get("id"), reason)
            return
        self._save(job, {"waiting_reason": reason})
        self._log(job, reason)

    def _stop(self, job, reason, done=False):
        self._save(job, {
            "status": "done" if done else "stopped",
            "stop_reason": None if done else reason,
        })
        self._log(job, reason)
        Notification.send("TWB noble job %s (%s|%s): %s" % (
            "finished" if done else "stopped",
            job.get("target_x"), job.get("target_y"), reason), category="attack")

    def _troops_at_home(self, village_id):
        return troops_at_home(village_id, wrapper=self.wrapper)

    def _target_owner(self, job):
        """The target's owner from the map cache ('0' = barb), or None when
        the village is not in the cache (yet)."""
        target_id = job.get("target_id")
        if not target_id:
            return None
        entry = FileManager.load_json_file("cache/villages/%s.json" % target_id)
        if not entry:
            return None
        owner = entry.get("owner")
        return str(owner) if owner is not None else None

    def _is_ours(self, village_id):
        return bool(FileManager.load_json_file("cache/managed/%s.json" % village_id))

    def _forced_peace_conflict(self, duration):
        """True when the send would land inside a forced-peace window."""
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

    # -- report evaluation -----------------------------------------------------

    def evaluate(self, job, reports):
        """Fold every fresh report on the target into the job: loyalty
        updates, conquest detection, and the troop-loss stops."""
        relevant = []
        for report_id, entry in reports.items():
            if not entry or str(entry.get("dest")) != str(job.get("target_id")):
                continue
            when = (entry.get("extra") or {}).get("when")
            if not when or int(when) <= int(job.get("eval_after") or 0):
                continue
            relevant.append((int(when), report_id, entry))

        for when, report_id, entry in sorted(relevant):
            extra = entry.get("extra") or {}
            sent = extra.get("units_sent") or {}
            losses = extra.get("units_losses") or {}
            loyalty = extra.get("loyalty")

            self._save(job, {"eval_after": when})

            if loyalty and len(loyalty) == 2:
                self._save(job, {"loyalty": {"value": int(loyalty[1]),
                                             "at": when}})
                self._log(job, "report: loyalty %s -> %s" % (
                    loyalty[0], loyalty[1]))
                if int(loyalty[1]) < 0:
                    self._stop(job, "conquered! loyalty fell to %s"
                               % loyalty[1], done=True)
                    return
            if sent:
                sent_nobles = int(sent.get("snob") or 0)
                dead_nobles = int(losses.get("snob") or 0)
                if all(int(losses.get(u, 0)) >= int(n or 0)
                       for u, n in sent.items() if int(n or 0) > 0):
                    self._stop(job, "red report - the whole send died")
                    return
                if sent_nobles and dead_nobles >= sent_nobles and not loyalty:
                    self._stop(job, "noble(s) died without the loyalty hit "
                                    "landing")
                    return

    # -- sending ---------------------------------------------------------------

    def _escort_available(self, job, home, nobles):
        return escort_packages(job, home, nobles)

    def _capture_noble_confirm(self, origin_id, x, y, units):
        """One-time capture of a confirm page containing a noble, so the
        in-game train/multi-send feature can be integrated later from what
        the server really renders. Never blocks the send."""
        try:
            marker = "cache/world/noble_confirm.json"
            if FileManager.path_exists(marker):
                return
            pre = self.wrapper.get_url(
                f"game.php?village={origin_id}&screen=place&target_type=coord")
            if not pre:
                return
            from core.extractors import Extractor
            data = {k: v for k, v in Extractor.attack_form(pre)}
            data.update({str(u): str(n) for u, n in units.items()})
            data.update({"x": x, "y": y, "target_type": "coord",
                         "attack": "Aanvallen"})
            conf = self.wrapper.post_url(
                url=f"game.php?village={origin_id}&screen=place&try=confirm",
                data=data)
            if conf and '<div class="error_box">' not in conf.text:
                FileManager.save_json_file(
                    {"html": conf.text, "_fetched": int(time.time())}, marker)
                logger.info("Captured a noble confirm page for train-feature "
                            "integration (%s)", marker)
        except Exception as exc:
            logger.debug("Noble confirm capture failed: %s", exc)

    def send(self, job, nobles, packages):
        """Fire `nobles` sequential noble attacks (the wrapper's own pacing
        spaces them a few seconds apart). Returns how many left."""
        sent = 0
        duration = 0
        for pack in packages:
            confirm_data, dur, err = attack_scheduler.prepare_command(
                self.wrapper, job.get("source_id"),
                job.get("target_x"), job.get("target_y"), pack)
            if err:
                self._log(job, "rally point: %s" % err)
                self._register_rejection(job, err)
                break
            duration = dur or duration
            if sent == 0 and self._forced_peace_conflict(dur):
                self._log(job, "send would land inside forced peace, holding")
                return 0
            ok, msg = attack_scheduler.fire_command(
                self.wrapper, job.get("source_id"), confirm_data)
            if not ok:
                self._log(job, "launch failed: %s" % msg)
                break
            sent += 1
            self._log(job, "noble %d/%d sent with %s (travel %ds)" % (
                sent, nobles, {u: n for u, n in pack.items()}, dur))
        if sent:
            now = int(time.time())
            totals = dict(job.get("totals") or {"sends": 0, "nobles": 0})
            totals["sends"] = int(totals.get("sends") or 0) + 1
            totals["nobles"] = int(totals.get("nobles") or 0) + sent
            self._save(job, {
                "reject_count": 0,
                "waiting_reason": None,
                "totals": totals,
                "in_flight": {
                    "nobles": sent,
                    "sent_at": now,
                    "back_at": now + 2 * int(duration or 0) + RETURN_MARGIN,
                },
                "last_sent": now,
            })
        return sent

    def _register_rejection(self, job, error):
        count = int(job.get("reject_count") or 0) + 1
        if count >= REJECT_STOP_AFTER:
            self._stop(job, "the game keeps refusing the send (%s)" % error)
        else:
            self._save(job, {"reject_count": count})

    def step(self, job, reports, now=None, flying_map=None, budget=None):
        """One cycle for one armed job: evaluate reports, check ownership,
        then send whatever the overshoot guard and the barracks allow.

        `budget` is focus_budgets()' cap for this job - the nobles left after
        every job above it in the list reserved what it could still need. None
        means no focus rule (send whatever the guard allows)."""
        now = now if now is not None else time.time()
        self.evaluate(job, reports)
        if job.get("status") != "armed":
            return 0

        owner = self._target_owner(job)
        if owner is not None and owner != "0":
            if self._is_ours(job.get("target_id")):
                self._stop(job, "conquered - the village is ours", done=True)
            else:
                self._stop(job, "another player took the village (owner %s)"
                           % owner)
            return 0

        in_flight = job.get("in_flight") or {}
        flying = int(in_flight.get("nobles") or 0)
        if flying and now < int(in_flight.get("back_at") or 0):
            return 0  # wait for the round trip + its reports
        if flying:
            self._save(job, {"in_flight": None})
            flying = 0

        # Nobles already on their way, whoever sent them. in_flight above only
        # covers the bot's own sends, so an attack launched by hand between two
        # cycles would otherwise be invisible: the bot sends its next noble,
        # the manual train takes the village first, and that noble dies on a
        # village that is already ours. Refusing to send while the overview is
        # unreadable costs a cycle; guessing costs a noble.
        if flying_map is None:
            self._waiting(job, "cannot read the command overview - holding off "
                               "until it is clear whether nobles are already "
                               "on their way to the target")
            return 0
        on_route = nobles_heading_to(job, flying_map)
        if on_route > flying:
            flying = on_route

        loyalty = estimate_loyalty(job, now=now)
        allowed = max_safe_nobles(loyalty) - flying
        if allowed <= 0:
            if on_route:
                self._waiting(job, "%d noble(s) already on the way to the "
                                   "target (loyalty est. %d allows %d) - "
                                   "waiting for them to land" % (
                                       on_route, loyalty,
                                       max_safe_nobles(loyalty)))
            return 0
        home = self._troops_at_home(job.get("source_id"))
        nobles_home = int(home.get("snob", 0) or 0)
        if nobles_home <= 0:
            self._waiting(job, "no nobleman at home in village %s - mint the "
                               "coins and recruit one (villages.%s.snobs is "
                               "how many the bot keeps)" % (
                                   job.get("source_id"), job.get("source_id")))
            return 0
        nobles = min(allowed, nobles_home)
        if budget is not None and budget < nobles:
            if budget <= 0:
                self._waiting(job, "holding: every noble at home is reserved "
                                   "for a target higher up the list")
                return 0
            nobles = budget
        packages, lacking = self._escort_available(job, home, nobles)
        if packages is None:
            self._waiting(job, "waiting for escort troops (%s)" % lacking)
            return 0
        self._capture_noble_confirm(
            job.get("source_id"), job.get("target_x"), job.get("target_y"),
            packages[0])
        self._log(job, "loyalty est. %d -> sending %d noble(s) "
                       "(guard allows %d, %d home)" % (
                           loyalty, nobles, allowed, nobles_home))
        sent = self.send(job, nobles, packages)
        if sent:
            # Keep the shared map honest for any later job aimed at the same
            # village; refetching it per job would be a request each.
            key = (int(job.get("target_x")), int(job.get("target_y")))
            flying_map[key] = flying_map.get(key, 0) + sent
        return sent

    def run(self):
        """One pass over all armed jobs."""
        jobs = load_jobs(self.path)
        armed = [j for j in jobs if j.get("status") == "armed"]
        if not armed:
            return 0
        # One account-wide request for the whole pass, forced fresh: the
        # reservation pass earlier in this cycle may have cached an older copy.
        flying_map = nobles_in_flight(
            wrapper=self.wrapper, village_id=armed[0].get("source_id"),
            force=True)
        reports = {}
        try:
            for name in FileManager.list_directory("cache/reports",
                                                   ends_with=".json"):
                reports[name[:-5]] = FileManager.load_json_file(
                    "cache/reports/%s" % name)
        except FileNotFoundError:
            pass

        # Priority pass before any send: work out what each job may take, so
        # the targets at the top of the list get first call on the nobles.
        # One rally-point read per source village, shared by its jobs.
        # With one armed job the budget can never bind - the worst-case claim
        # is always at least the guard's train size - so skip the extra
        # rally-point read that computing it would cost.
        budgets = {}
        if len(armed) > 1 and (self.config.get("farms") or {}).get(
                "noble_focus_fire", True):
            home_by_source = {}
            for job in armed:
                source = str(job.get("source_id"))
                if source not in home_by_source:
                    home_by_source[source] = self._troops_at_home(source)
            budgets = focus_budgets(armed, flying_map=flying_map,
                                    home_by_source=home_by_source)

        sent = 0
        for job in armed:
            try:
                sent += self.step(job, reports, flying_map=flying_map,
                                  budget=budgets.get(job.get("id")))
            except Exception as exc:  # one broken job must not kill the loop
                logger.warning("Noble job %s failed this cycle: %s",
                               job.get("id"), exc)
        return sent
