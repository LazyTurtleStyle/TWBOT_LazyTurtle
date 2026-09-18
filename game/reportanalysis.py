"""Read the reports back out and write what they mean onto the map. ALPHA.

A report is read once, on the day it arrives, and then it is gone - filed into
a folder nobody opens again. But the interesting thing about a report is rarely
interesting on the day: it becomes interesting weeks later, when the same
player comes back.

The one this does: an attack that dies takes the attacker's nuke with it, and
rebuilding one takes weeks. So every defence that held is a fact about the next
wave - that village cannot be part of it. Fifty-three of them were sitting in
this account's reports, one per report, going back to the first of September,
and by the time the next wave lands nobody remembers which of two hundred
villages they were.

So the answer is written where it will actually be read: the private note the
game keeps on every village's info page, which is on screen anyway the moment
you click an incoming.

  - It only ever adds. The current note is read first and the new line goes
    above whatever is there, because a note written by hand is exactly what is
    not recorded anywhere else.
  - A village already carrying the line is left alone, and the villages it has
    written are remembered, so a second pass costs nothing and says nothing.
  - It never runs on its own. The Report analysis page queues one job - the
    rows it is showing, with the settings it is showing them with - and the
    bot does that job once, on its next cycle, then goes back to costing
    nothing. Reading five thousand reports every cycle to find the two new
    villages a wave produced is not worth the time it takes.
  - Marked alpha: it writes to the game, and what it writes is an opinion
    about a report.
"""

import json
import logging
import os
import re
import time

from core.filemanager import FileManager
from game import villagenotes, worldvillages
from game.attack_scheduler import _Lock

logger = logging.getLogger("ReportAnalysis")

STATE_FILE = "cache/report_analysis.json"

# A nuke that dies is gone for weeks; anything under this is a probe, a fake or
# a snipe-bait rather than a clear.
DEFAULT_MIN_UNITS = 5000
# Losses are near-binary in practice - on the account this was built against,
# of the incoming attacks over 2000 units, 41 lost everything and 52 lost under
# half, with a single 70% in between - so this only has to separate two piles.
DEFAULT_MIN_LOSS_PCT = 90
# And how little it can have lost to count as having walked through. The middle
# ground - a nuke that lost most of itself but not all - is left out of both
# piles on purpose: it is neither gone nor intact, and guessing which would put
# a wrong word on the map. There was exactly one such report in the account
# this was built against.
DEFAULT_ALIVE_MAX_LOSS_PCT = 50
DEFAULT_NOTE_PREFIX = "clear dood"
DEFAULT_NOTE_PREFIX_ALIVE = "clear leeft"
# A dead nuke does not stay dead. Nobody on this account has come back from one
# yet - every village that hit twice was alive both times - so there is no
# measured figure; two weeks is the player's own estimate of a rebuild, and the
# note carries the date it runs out so it can be read without doing sums.
DEFAULT_REBUILD_DAYS = 14
DEFAULT_NOTE_REBUILD = "herbouwd ~"

# What a village is for, read off the troops it has shown. Spies, the paladin
# and nobles say nothing about it and are left out of the count; heavy cavalry
# is on the defensive side because that is how it is almost always built.
OFFENSIVE_UNITS = ("axe", "light", "marcher", "ram", "catapult")
DEFENSIVE_UNITS = ("spear", "sword", "archer", "heavy")
# How much of a stack has to be one side before it names the village, and how
# big it has to be to be worth reading at all - a farm run of a few light
# cavalry is not an offensive village.
KIND_SHARE = 0.7
KIND_MIN_UNITS = 1000
# Below that a stack is a fake, and a fake names its village only by what is
# in it. Axes, light and mounted archers are built in offensive villages and
# nowhere else, so any of them in an incoming attack is proof enough. Rams,
# catapults, scouts and heavy cavalry are not: every village has them, and a
# ram fake from a defensive village is exactly how a nuke gets hidden - on the
# account this was built against 46 villages faked with nothing else, against
# 54 that sent axes or light along.
OFFENSIVE_SIGNATURE = ("axe", "light", "marcher")
# Which rows a job writes, by the page's "Clear" filter.
VIEWS = ("dead", "back", "alive", "none", "")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _state_path(path=None):
    """Where the state lives. The dashboard passes its own: its process has no
    per-world data root."""
    return path or FileManager.get_path(STATE_FILE)


def load_state(path=None):
    try:
        with open(_state_path(path)) as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def update_state(mutator, path=None):
    """Read -> mutate -> write under the cross-process lock. The dashboard
    queues jobs and records the notes it writes by hand into the same file the
    bot records its job in, and neither may lose the other's write."""
    target = _state_path(path)
    with _Lock(target):
        state = load_state(target)
        mutator(state)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = "%s.tmp.%d" % (target, os.getpid())
        with open(tmp, "w") as handle:
            json.dump(state, handle, indent=1)
        os.replace(tmp, target)
    return state


def in_view(row, view):
    """Whether a row is one the page's "Clear" filter is showing - the same
    test as the page's own, so a job writes exactly what was on screen."""
    if not view:
        return True
    if view == "back":
        return row["state"] == "dead" and bool(row.get("maybe_rebuilt"))
    if view == "dead":
        return row["state"] == "dead" and not row.get("maybe_rebuilt")
    return row["state"] == view


def queue_job(job, path=None):
    """Ask the bot to write one batch of notes on its next cycle. `job` is the
    page's selection: view, owner, and the thresholds and words it used."""
    job = dict(job or {})
    job["requested"] = int(time.time())
    job["status"] = "queued"
    return update_state(lambda state: state.update({"job": job}), path)


def record_noted(village_id, line, path=None):
    """Remember a note written by hand from the page, so a job does not spend
    a page read finding out it is already there."""
    return update_state(
        lambda state: state.setdefault("noted", {}).update(
            {str(village_id): line}), path)


def troop_kind(units):
    """"OFF", "DEF" or None for a set of troops."""
    counts = {k: _int(v) for k, v in (units or {}).items()}
    off = sum(counts.get(u, 0) for u in OFFENSIVE_UNITS)
    dfn = sum(counts.get(u, 0) for u in DEFENSIVE_UNITS)
    if off + dfn < KIND_MIN_UNITS:
        return None
    if off >= KIND_SHARE * (off + dfn):
        return "OFF"
    if dfn >= KIND_SHARE * (off + dfn):
        return "DEF"
    return None


def village_readings(reports, managed):
    """What every enemy village has shown of itself, per village id.

    Two kinds of sighting, each kept newest-first:

      - "sent": a stack it sent at us (units_sent of an incoming attack);
      - "home": what one of our scouts or attacks found standing in it
        (defence_units - which includes any support parked there, so a DEF
        reading can be a friend's troops rather than its own).

    `kind` is the newest sighting that names one side clearly. A village is
    built for one job and keeps it, so the newest answer is the answer; the
    older readings are kept for the dashboard to show.
    """
    managed = managed or {}
    seen = {}
    for report in (reports or {}).values():
        if report.get("type") not in ("attack", "scout"):
            continue
        origin, dest = str(report.get("origin")), str(report.get("dest"))
        extra = report.get("extra") or {}
        when = _int(extra.get("when"))
        if origin in managed and dest not in managed and dest != "None":
            units = extra.get("defence_units") or {}
            village, how = dest, "home"
        elif origin not in managed and origin != "None" \
                and report.get("type") == "attack":
            units = extra.get("units_sent") or {}
            village, how = origin, "sent"
        else:
            continue
        kind = troop_kind(units)
        if kind is None and how == "sent" and \
                any(_int(units.get(u)) for u in OFFENSIVE_SIGNATURE):
            kind = "OFF"
        entry = seen.setdefault(village, {"sightings": []})
        entry["sightings"].append({
            "when": when, "how": how,
            "total": sum(_int(n) for n in units.values()),
            "kind": kind})
    for entry in seen.values():
        entry["sightings"].sort(key=lambda s: s["when"], reverse=True)
        entry["kind"] = next((s["kind"] for s in entry["sightings"]
                              if s["kind"]), None)
    return seen


def _date(stamp, fmt="%d-%m-%Y"):
    return time.strftime(fmt, time.localtime(stamp)) if stamp else ""


def find_clear_states(reports, managed, village_db, world,
                     min_units=DEFAULT_MIN_UNITS,
                     min_loss_pct=DEFAULT_MIN_LOSS_PCT,
                     alive_max_loss_pct=DEFAULT_ALIVE_MAX_LOSS_PCT,
                     rebuild_days=DEFAULT_REBUILD_DAYS, now=None):
    """Every enemy village that has thrown a real stack at us, and what became
    of it, newest event first.

    Each village gets one row carrying `state`:

        "dead"  - its nuke broke on a wall and has to be rebuilt, which is
                  weeks; that village cannot be in the next wave
        "alive" - it has walked a nuke through and nothing has killed it since
        "none"  - no clear of its has been seen, but its troops say what it
                  is: fakes with axes or light in them, or what a scout found
                  standing there. Only the type is known, so only the type is
                  said.

    Which one it is follows the reports rather than a guess: the newest death
    is compared against the newest survival, so a village that lost a clear and
    later rebuilt and attacked again reads as alive from the day it did. No
    village on the account this was built against had both yet - 53 dead, 66
    alive, none in between - but the first rebuild makes the comparison the
    whole answer, and a note that says "dood" about a village that has since
    walked a nuke through is worse than no note.

    "On us" is judged by the attacking village not being one of ours, NOT by
    the attacked one still being: a village that held and was taken later still
    killed the nuke, and requiring the target to still be ours threw away
    thirteen of fifty-three here.

    `world` is the world's public village list, which is what supplies a name,
    a position and an owner for the attackers outside the bot's own map cache -
    most of them, since that cache only covers the ground around our villages.

    A dead clear is not dead forever. `rebuild_days` after the death it is
    probably back, so a dead row also carries `rebuilt_by` (that moment),
    `maybe_rebuilt` (whether it has passed) and `seen_since`: the newest time
    one of our scouts or attacks looked inside the village after the death,
    with how many troops were standing there - the one reading that says
    whether it actually is back, rather than whether it could be.
    """
    now = now or int(time.time())
    readings = village_readings(reports, managed)
    by_village = {}
    for report in (reports or {}).values():
        if report.get("type") != "attack":
            continue
        origin, dest = str(report.get("origin")), str(report.get("dest"))
        if not origin or origin == "None" or origin in managed:
            continue
        extra = report.get("extra") or {}
        sent = sum(_int(n) for n in (extra.get("units_sent") or {}).values())
        lost = sum(_int(n) for n in (extra.get("units_losses") or {}).values())
        if not sent or sent < min_units:
            continue
        pct = round(100.0 * lost / sent)
        if pct >= min_loss_pct:
            state = "dead"
        elif pct <= alive_max_loss_pct:
            state = "alive"
        else:
            continue            # neither gone nor intact; say nothing
        when = _int(extra.get("when"))
        previous = by_village.get(origin)
        if previous and previous["when"] >= when:
            # An older report can still be the newest of ITS kind, and that is
            # what decides the verdict, so the dates are kept either way.
            previous["when_" + state] = max(previous.get("when_" + state, 0), when)
            previous["state"] = ("dead" if previous.get("when_dead", 0)
                                 > previous.get("when_alive", 0) else "alive")
            continue
        known = (village_db or {}).get(origin) or {}
        out = (world or {}).get(origin) or {}
        coords = known.get("location")
        if not coords and out.get("x") is not None:
            coords = [out["x"], out["y"]]
        target = (managed or {}).get(dest) or (village_db or {}).get(dest) or {}
        by_village[origin] = {
            "village_id": origin,
            "name": out.get("name") or known.get("name"),
            "coords": coords,
            "points": out.get("points") or known.get("points"),
            "owner": out.get("owner")
                     or (str(known.get("owner")) if known.get("owner") else None),
            "when": when,
            "date": (time.strftime("%d-%m-%Y", time.localtime(when))
                     if when else ""),
            "sent": sent,
            "lost": lost,
            "loss_pct": pct,
            "target_id": dest,
            "target_name": (target.get("name")
                            or (target.get("public") or {}).get("name") or dest),
            # Whether the village it died on is still ours. It counts either
            # way; saying which is the difference between a list and a story.
            "target_held": dest in (managed or {}),
            "state": state,
            "when_dead": when if state == "dead" else
                         (by_village.get(origin, {}).get("when_dead", 0)),
            "when_alive": when if state == "alive" else
                          (by_village.get(origin, {}).get("when_alive", 0)),
        }
        row = by_village[origin]
        row["state"] = "dead" if row["when_dead"] > row["when_alive"] else "alive"
    for row in by_village.values():
        # The date on the note is the date of the event it describes, which is
        # not always this row's newest report: a village that died and later
        # walked one through is alive as of the survival, not of the death.
        stamp = row["when_dead"] if row["state"] == "dead" else row["when_alive"]
        row["date"] = _date(stamp) or row["date"]
        seen = readings.get(row["village_id"]) or {}
        # Every row here threw a clear at us, so OFF unless the troops say
        # otherwise; the reading is still taken so a village that turns out to
        # be something else is called what it is.
        row["kind"] = seen.get("kind") or "OFF"
        row["age_days"] = max(0, (now - stamp) // 86400) if stamp else None
        row["rebuilt_by"] = row["rebuilt_date"] = None
        row["maybe_rebuilt"] = False
        row["seen_since"] = None
        if row["state"] == "dead":
            if rebuild_days and rebuild_days > 0:
                row["rebuilt_by"] = stamp + int(rebuild_days) * 86400
                row["rebuilt_date"] = _date(row["rebuilt_by"], "%d-%m")
                row["maybe_rebuilt"] = now >= row["rebuilt_by"]
            look = next((s for s in seen.get("sightings", [])
                         if s["how"] == "home" and s["when"] > stamp), None)
            if look:
                row["seen_since"] = {"date": _date(look["when"]),
                                     "total": look["total"],
                                     "kind": look["kind"]}
    # The villages that never sent a clear but have shown what they are.
    for village_id, seen in readings.items():
        if village_id in by_village or not seen.get("kind"):
            continue
        out = (world or {}).get(village_id) or {}
        known = (village_db or {}).get(village_id) or {}
        # Barbarians are scouted and farmed all day; they are not anybody's
        # offence or defence.
        if str(out.get("owner", "")) == "0":
            continue
        latest = next(s for s in seen["sightings"] if s["kind"])
        coords = known.get("location")
        if not coords and out.get("x") is not None:
            coords = [out["x"], out["y"]]
        by_village[village_id] = {
            "village_id": village_id,
            "name": out.get("name") or known.get("name"),
            "coords": coords,
            "points": out.get("points") or known.get("points"),
            "owner": out.get("owner")
                     or (str(known.get("owner")) if known.get("owner") else None),
            "when": latest["when"], "date": _date(latest["when"]),
            "sent": latest["total"], "lost": None, "loss_pct": None,
            "seen_as": latest["how"],
            "target_id": None, "target_name": "", "target_held": True,
            "state": "none", "kind": seen["kind"],
            "age_days": max(0, (now - latest["when"]) // 86400)
                        if latest["when"] else None,
            "rebuilt_by": None, "rebuilt_date": None,
            "maybe_rebuilt": False, "seen_since": None,
        }
    return sorted(by_village.values(), key=lambda r: r["when"], reverse=True)


def find_dead_clears(reports, managed, village_db, world,
                     min_units=DEFAULT_MIN_UNITS,
                     min_loss_pct=DEFAULT_MIN_LOSS_PCT):
    """Only the villages whose clear is currently dead."""
    return [r for r in find_clear_states(reports, managed, village_db, world,
                                         min_units, min_loss_pct)
            if r["state"] == "dead"]


def note_line(row, prefix=DEFAULT_NOTE_PREFIX, prefix_alive=DEFAULT_NOTE_PREFIX_ALIVE,
              rebuild_word=DEFAULT_NOTE_REBUILD):
    """What to write on the village: what it is, what became of its clear, and
    when - and for a dead one, when it is probably back.

        OFF - clear dood 12-09-2026 (herbouwd ~26-09)

    The rebuild date is written rather than worked out later, so the note
    never has to be rewritten to stay true: it says when to stop trusting it.
    A village whose clear has never been seen gets its type and nothing else.
    """
    if row.get("state") == "none":
        return row.get("kind") or ""
    word = prefix_alive if row.get("state") == "alive" else prefix
    line = ("%s %s" % ((word or "").strip(), row.get("date", ""))).strip()
    if row.get("kind"):
        line = "%s - %s" % (row["kind"], line)
    if row.get("state") == "dead" and row.get("rebuilt_date"):
        line = "%s (%s%s)" % (line, rebuild_word or "", row["rebuilt_date"])
    return line


def own_line_matcher(prefixes):
    """A test for "this note line was written by this module", so a village
    whose verdict changed gets its old line replaced instead of a second one
    stacked on top. Deliberately narrow: the whole line has to be one of ours
    - optional kind, one of the prefixes, a date, an optional bracket - so a
    line somebody typed by hand is never taken for one."""
    words = sorted({(p or "").strip() for p in prefixes if (p or "").strip()},
                   key=len, reverse=True)
    bare = re.compile(r"^(?:OFF|DEF)$")
    if not words:
        return lambda line: bool(bare.match((line or "").strip()))
    pattern = re.compile(
        r"^(?:[A-Z?]{2,6} - )?(?:%s) \d{2}-\d{2}-\d{4}(?: \([^)]*\))?$"
        % "|".join(re.escape(w) for w in words), re.I)
    # A bare type line is ours too, so the day a clear is seen it becomes
    # "OFF - clear dood ..." rather than sitting under it.
    return lambda line: bool(pattern.match((line or "").strip())
                             or bare.match((line or "").strip()))


def own_prefixes(settings):
    """Every prefix a line of ours could have been written with: the current
    ones and the defaults, old capitalised ones included by the matcher being
    case-blind."""
    settings = settings or {}
    return [settings.get("note_prefix"), settings.get("note_prefix_alive"),
            DEFAULT_NOTE_PREFIX, DEFAULT_NOTE_PREFIX_ALIVE]


# -- writing, through the bot's own wrapper ---------------------------------

def read_note(wrapper, home_village, village_id):
    """The note on a village right now, via the bot's session."""
    res = wrapper.get_url("game.php?village=%s&screen=info_village&id=%s"
                          % (home_village, village_id))
    text = getattr(res, "text", "") if res is not None else ""
    if not text:
        return None
    return villagenotes._note_text(text)


def write_note(wrapper, home_village, village_id, note):
    """Save a note, replacing what is there. Callers merge first."""
    result = wrapper.get_api_action(
        village_id=home_village, action="village_note_edit",
        data={"village_id": str(village_id), "note": note})
    if not isinstance(result, dict):
        return False
    return not result.get("error")


def add_note(wrapper, home_village, village_id, line, replace=None):
    """Add one line to a village's note, keeping what was there - except an
    older line of this module's own, which `replace` recognises and which the
    new one takes the place of.

    Returns "written", "already" or None; None means the note could not be read
    and so was not touched - an unreadable note and an empty one are not the
    same thing, and the difference is somebody's notes.
    """
    current = read_note(wrapper, home_village, village_id)
    if current is None:
        return None
    merged = villagenotes.compose(current, line, replace)
    if merged is None:
        return "already"
    return "written" if write_note(wrapper, home_village, village_id, merged) else None


def _load_cache_dir(path):
    """Every json file in one of the bot's cache directories, keyed by its id."""
    try:
        names = FileManager.list_directory(path, ends_with=".json")
    except (FileNotFoundError, OSError):
        return {}
    out = {}
    for name in names:
        entry = FileManager.load_json_file("%s/%s" % (path, name))
        if entry:
            out[name[:-len(".json")]] = entry
    return out


def _job_settings(job, settings):
    """The job's own values where the page sent them, the config otherwise."""
    def pick(key, default):
        value = job.get(key)
        return value if value not in (None, "") else settings.get(key, default)
    return pick


def run(wrapper, home_village, config):
    """Serve a queued job, if there is one. Otherwise do nothing at all - not
    even read the reports - so a cycle with no job costs one small file read.

    A job writes every row of the page's selection that does not already carry
    its line, in one go, paced by the bot's own wrapper like any other request.
    Progress is saved as it goes so the page can show it.
    """
    state = load_state()
    job = state.get("job") or {}
    if job.get("status") != "queued" or not home_village:
        return 0
    settings = (config or {}).get("report_analysis", {}) or {}
    pick = _job_settings(job, settings)
    prefix = pick("prefix", DEFAULT_NOTE_PREFIX)
    prefix_alive = pick("prefix_alive", DEFAULT_NOTE_PREFIX_ALIVE)
    rebuild_word = job.get("rebuild_word") if job.get("rebuild_word") is not None \
        else settings.get("note_rebuild", DEFAULT_NOTE_REBUILD)
    ours = own_line_matcher([prefix, prefix_alive] + own_prefixes(settings))
    rows = find_clear_states(
        _load_cache_dir("cache/reports"),
        config.get("villages", {}) or {},
        _load_cache_dir("cache/villages"),
        worldvillages.cached(wrapper),
        _int(pick("min_units", DEFAULT_MIN_UNITS)) or DEFAULT_MIN_UNITS,
        _int(pick("min_loss_pct", DEFAULT_MIN_LOSS_PCT)) or DEFAULT_MIN_LOSS_PCT,
        _int(pick("alive_max_loss_pct", DEFAULT_ALIVE_MAX_LOSS_PCT))
        or DEFAULT_ALIVE_MAX_LOSS_PCT,
        _int(pick("rebuild_days", DEFAULT_REBUILD_DAYS)))
    view = job.get("view") if job.get("view") in VIEWS else ""
    owner = str(job.get("owner") or "")
    rows = [r for r in rows if in_view(r, view)
            and (not owner or str(r.get("owner")) == owner)]
    noted = state.get("noted") or {}
    todo = []
    for row in rows:
        line = note_line(row, prefix, prefix_alive, rebuild_word)
        # Remembered rather than re-checked: the check itself costs a page.
        if line and noted.get(row["village_id"]) != line:
            todo.append((row, line))

    progress = {"status": "running", "started": int(time.time()),
                "selected": len(rows), "total": len(todo),
                "written": 0, "already": 0, "failed": 0}
    done = {}

    def save(extra=None):
        def apply(st):
            st.setdefault("noted", {}).update(done)
            current = st.get("job") or {}
            # A newer press while this ran is kept, not overwritten.
            if current.get("requested") == job.get("requested"):
                current.update(progress)
                current.update(extra or {})
                st["job"] = current
        update_state(apply)

    save()
    logger.info("Report analysis job: %d of %d selected villages to note",
                len(todo), len(rows))
    for n, (row, line) in enumerate(todo, 1):
        outcome = add_note(wrapper, home_village, row["village_id"], line, ours)
        if outcome is None:
            progress["failed"] += 1
            logger.debug("Could not read the note on %s, leaving it alone",
                         row["village_id"])
        else:
            progress["already" if outcome == "already" else "written"] += 1
            done[row["village_id"]] = line
            if outcome == "written":
                logger.info("Noted %s (%s): %s",
                            row.get("name") or row["village_id"],
                            "%s|%s" % tuple(row["coords"]) if row.get("coords") else "?",
                            line)
        if n % 10 == 0:
            save()
    progress["status"] = "done"
    save({"finished": int(time.time())})
    update_state(lambda st: st.update({"last_run": int(time.time())}))
    return progress["written"]
