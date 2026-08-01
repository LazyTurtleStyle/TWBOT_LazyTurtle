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

logger = logging.getLogger("NobleBarb")


def _resolve(path):
    return path or FileManager.get_path(NOBLE_JOBS_FILE)


def load_jobs(path=None):
    """Current job list (always a list)."""
    return attack_scheduler.load_schedule(path=_resolve(path))


def _update(mutator, path=None):
    return attack_scheduler.update(mutator, path=_resolve(path))


def add_job(entry, path=None):
    """Append a new noble job and return it (with an assigned id).

    Jobs start paused - the user arms them explicitly from the dashboard."""
    entry.setdefault("status", "paused")
    entry.setdefault("eval_after", int(time.time()))
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


def planned_send(job, now=None, wrapper=None, home=None):
    """What this job would send if the noble pass ran right now.
    Returns (nobles, packages) or (0, None).

    This is the same decision NobleBarbManager.step() makes, minus the parts
    that need this cycle's reports (loyalty may still drop before the real
    send, which can only make the plan smaller) and minus the ownership stops
    (a target that flipped owner reserves troops for one last cycle). `home`
    is the sending village's troops; without it they are read through
    troops_at_home(wrapper)."""
    if job.get("status") != "armed":
        return 0, None
    now = now if now is not None else time.time()
    in_flight = job.get("in_flight") or {}
    flying = int(in_flight.get("nobles") or 0)
    if flying and now < int(in_flight.get("back_at") or 0):
        return 0, None  # the train is still out; nothing to hold back
    allowed = max_safe_nobles(estimate_loyalty(job, now=now))
    if allowed <= 0:
        return 0, None
    if home is None:
        home = troops_at_home(job.get("source_id"), wrapper=wrapper)
    nobles_home = int(home.get("snob", 0) or 0)
    if nobles_home <= 0:
        return 0, None
    nobles = min(allowed, nobles_home)
    packages, _lacking = escort_packages(job, home, nobles)
    if packages is None:
        return 0, None
    return nobles, packages


def escort_reservations(jobs=None, path=None, now=None, wrapper=None):
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
    for job in (jobs if jobs is not None else load_jobs(path)):
        source = str(job.get("source_id"))
        if job.get("status") == "armed" and source not in home_by_village:
            home_by_village[source] = troops_at_home(source, wrapper=wrapper)
        try:
            _nobles, packages = planned_send(
                job, now=now, home=home_by_village.get(source))
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

    def step(self, job, reports, now=None):
        """One cycle for one armed job: evaluate reports, check ownership,
        then send whatever the overshoot guard and the barracks allow."""
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

        loyalty = estimate_loyalty(job, now=now)
        allowed = max_safe_nobles(loyalty) - flying
        if allowed <= 0:
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
        return self.send(job, nobles, packages)

    def run(self):
        """One pass over all armed jobs."""
        jobs = load_jobs(self.path)
        if not any(j.get("status") == "armed" for j in jobs):
            return 0
        reports = {}
        try:
            for name in FileManager.list_directory("cache/reports",
                                                   ends_with=".json"):
                reports[name[:-5]] = FileManager.load_json_file(
                    "cache/reports/%s" % name)
        except FileNotFoundError:
            pass
        sent = 0
        for job in jobs:
            if job.get("status") != "armed":
                continue
            try:
                sent += self.step(job, reports)
            except Exception as exc:  # one broken job must not kill the loop
                logger.warning("Noble job %s failed this cycle: %s",
                               job.get("id"), exc)
        return sent
