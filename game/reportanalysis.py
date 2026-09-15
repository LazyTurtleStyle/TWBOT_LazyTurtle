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
  - It is off by default and does not turn itself on. Marked alpha: it writes
    to the game, and what it writes is an opinion about a report.
"""

import logging
import time

from core.filemanager import FileManager
from game import villagenotes, worldvillages

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
DEFAULT_NOTE_PREFIX = "Clear dood"
DEFAULT_NOTE_PREFIX_ALIVE = "Clear leeft"
# Each note is two requests (read the page, save the note). A first run on a
# long war is fifty of them, which is a lot of traffic in one burst, so a pass
# takes a bounded bite and the next pass continues.
DEFAULT_MAX_PER_RUN = 10


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_state():
    return FileManager.load_json_file(STATE_FILE) or {}


def save_state(state):
    FileManager.save_json_file_atomic(state, STATE_FILE)


def find_clear_states(reports, managed, village_db, world,
                     min_units=DEFAULT_MIN_UNITS,
                     min_loss_pct=DEFAULT_MIN_LOSS_PCT,
                     alive_max_loss_pct=DEFAULT_ALIVE_MAX_LOSS_PCT):
    """Every enemy village that has thrown a real stack at us, and what became
    of it, newest event first.

    Each village gets one row carrying `state`:

        "dead"  - its nuke broke on a wall and has to be rebuilt, which is
                  weeks; that village cannot be in the next wave
        "alive" - it has walked a nuke through and nothing has killed it since

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
    """
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
        row["date"] = (time.strftime("%d-%m-%Y", time.localtime(stamp))
                       if stamp else row["date"])
    return sorted(by_village.values(), key=lambda r: r["when"], reverse=True)


def find_dead_clears(reports, managed, village_db, world,
                     min_units=DEFAULT_MIN_UNITS,
                     min_loss_pct=DEFAULT_MIN_LOSS_PCT):
    """Only the villages whose clear is currently dead."""
    return [r for r in find_clear_states(reports, managed, village_db, world,
                                         min_units, min_loss_pct)
            if r["state"] == "dead"]


def note_line(row, prefix=DEFAULT_NOTE_PREFIX, prefix_alive=DEFAULT_NOTE_PREFIX_ALIVE):
    """What to write on the village: what became of its clear, and when."""
    word = prefix_alive if row.get("state") == "alive" else prefix
    return ("%s %s" % ((word or "").strip(), row.get("date", ""))).strip()


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


def add_note(wrapper, home_village, village_id, line):
    """Add one line to a village's note, keeping what was there.

    Returns "written", "already" or None; None means the note could not be read
    and so was not touched - an unreadable note and an empty one are not the
    same thing, and the difference is somebody's notes.
    """
    current = read_note(wrapper, home_village, village_id)
    if current is None:
        return None
    merged = villagenotes.compose(current, line)
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


def run(wrapper, home_village, config):
    """One pass: note the dead clears that have not been noted yet.

    Off unless report_analysis.enabled is set. Never turns itself on, and takes
    a bounded number of villages per pass so a first run on a long war does not
    fire a hundred requests in a burst. Loads its own inputs, so the caller has
    nothing to assemble.
    """
    settings = (config or {}).get("report_analysis", {}) or {}
    if not settings.get("enabled", False) or not home_village:
        return 0
    prefix = settings.get("note_prefix", DEFAULT_NOTE_PREFIX)
    prefix_alive = settings.get("note_prefix_alive", DEFAULT_NOTE_PREFIX_ALIVE)
    rows = find_clear_states(
        _load_cache_dir("cache/reports"),
        config.get("villages", {}) or {},
        _load_cache_dir("cache/villages"),
        worldvillages.cached(wrapper),
        _int(settings.get("min_units")) or DEFAULT_MIN_UNITS,
        _int(settings.get("min_loss_pct")) or DEFAULT_MIN_LOSS_PCT,
        _int(settings.get("alive_max_loss_pct"))
        or DEFAULT_ALIVE_MAX_LOSS_PCT)
    # Which of the two the module writes. A dead clear is the one that changes
    # what you do about the next wave, so it is the one that is on by default;
    # noting the living ones marks out the rest of his hitting power and is
    # asked for separately.
    if not settings.get("note_alive", False):
        rows = [r for r in rows if r["state"] == "dead"]
    if not rows:
        return 0
    cap = _int(settings.get("max_per_run")) or DEFAULT_MAX_PER_RUN
    state = load_state()
    noted = state.setdefault("noted", {})
    done = 0
    for row in rows:
        if done >= cap:
            break
        line = note_line(row, prefix, prefix_alive)
        # Remembered rather than re-checked: the check itself costs a page.
        if noted.get(row["village_id"]) == line:
            continue
        outcome = add_note(wrapper, home_village, row["village_id"], line)
        if outcome is None:
            logger.debug("Could not read the note on %s, leaving it alone",
                         row["village_id"])
            continue
        noted[row["village_id"]] = line
        done += 1
        if outcome == "written":
            logger.info("Noted %s (%s): %s",
                        row.get("name") or row["village_id"],
                        "%s|%s" % tuple(row["coords"]) if row.get("coords") else "?",
                        line)
    if done:
        state["last_run"] = int(time.time())
        save_state(state)
    return done
