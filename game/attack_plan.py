"""
Import an attack plan produced elsewhere (devilicious.dev and the BB-code
exports that follow the same layout) into scheduled commands.

One planned command per line:

    569|444->564|454,11.18,axe,Attack,2026-08-14 08:03:00.000,03:21:14.000,2026-08-14 04:41:46.000,1
    origin  target  dist  unit type  arrival                  travel        send                   count

Only the coordinates, the arrival moment and the count actually drive anything:
the bot re-derives travel from the world's own unit speeds, and the send moment
follows from the arrival. The plan's distance/travel/send columns are kept so
the dashboard can show them next to ours - a mismatch means the plan was built
for different world settings, which is worth seeing before queueing 30 attacks.

A plan never says which troops to send (it only names the unit that paces the
command), so the units are chosen in the dashboard before anything is queued.
"""
import datetime
import re

# One planned line. Written to be found inside surrounding markup rather than to
# match a whole line, so BB-code table rows and forum quoting parse as-is.
_RE_ROW = re.compile(r"""
    (?P<ox>\d{1,3})\s*\|\s*(?P<oy>\d{1,3})
    \s*-+>\s*
    (?P<tx>\d{1,3})\s*\|\s*(?P<ty>\d{1,3})
    \s*,\s*(?P<distance>[\d.]+)
    \s*,\s*(?P<unit>[a-zA-Z_]+)
    \s*,\s*(?P<kind>[a-zA-Z_]+)
    \s*,\s*(?P<arrival>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.:]\d{1,3})?)
    \s*,\s*(?P<travel>\d{1,4}:\d{2}:\d{2}(?:[.:]\d{1,3})?)
    \s*,\s*(?P<send>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.:]\d{1,3})?)
    (?:\s*,\s*(?P<count>\d{1,3}))?
""", re.VERBOSE)

# What the plan's type column means for the command we send. Anything else is
# reported rather than guessed at - sending an attack where a plan said support
# is not a mistake worth making automatically.
KINDS = {
    "attack": "attack",
    "aanval": "attack",
    "support": "support",
    "ondersteuning": "support",
    "fake": "attack",
    "nuke": "attack",
    "noble": "attack",
    "adel": "attack",
}


def _timestamp(text):
    """Unix seconds for a 'YYYY-MM-DD HH:MM:SS(.mmm)' stamp, read as host time."""
    text = text.strip().replace("T", " ")
    stamp, _, fraction = text.partition(".")
    if not fraction:
        stamp, sep, fraction = text.rpartition(":")
        # A trailing ":109" is milliseconds in the game's own notation, but only
        # when the part before it already looks like a full clock time.
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", stamp):
            stamp, fraction = text, ""
    when = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    millis = int((fraction or "0").ljust(3, "0")[:3])
    return when.timestamp() + millis / 1000.0


def _duration(text):
    """Seconds for a 'H:MM:SS(.mmm)' travel time."""
    text = text.strip()
    head, _, fraction = text.partition(".")
    hours, minutes, seconds = (int(p) for p in head.split(":")[:3])
    return hours * 3600 + minutes * 60 + seconds + int((fraction or "0")[:3] or 0) / 1000.0


def parse_plan(text):
    """Read a pasted plan. Returns (rows, skipped).

    `rows` are dicts with origin/target coordinate pairs, the paced unit, the
    command kind, arrival/send unix timestamps, the plan's own travel time and
    how many commands the line asks for. `skipped` holds the lines that carried
    something command-shaped but could not be read, so the dashboard can say
    which ones were ignored instead of silently dropping them.
    """
    rows, skipped = [], []
    for number, line in enumerate((text or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        match = _RE_ROW.search(stripped)
        if not match:
            # Only complain about lines that look like they meant to be a
            # command; table markup and headers are expected noise.
            if "->" in stripped and re.search(r"\d\|\d", stripped):
                skipped.append({"line": number, "text": stripped[:120],
                                "reason": "could not read the columns"})
            continue
        data = match.groupdict()
        try:
            arrival = _timestamp(data["arrival"])
            send = _timestamp(data["send"])
            travel = _duration(data["travel"])
        except (ValueError, TypeError) as exc:
            skipped.append({"line": number, "text": stripped[:120],
                            "reason": "bad date or duration (%s)" % exc})
            continue
        kind = KINDS.get(data["kind"].lower())
        if not kind:
            skipped.append({"line": number, "text": stripped[:120],
                            "reason": "unknown command type '%s'" % data["kind"]})
            continue
        rows.append({
            "line": number,
            "origin": [int(data["ox"]), int(data["oy"])],
            "target": [int(data["tx"]), int(data["ty"])],
            "distance": float(data["distance"]),
            "unit": data["unit"].lower(),
            "kind": kind,
            "kind_raw": data["kind"],
            "arrival_ts": arrival,
            "send_ts": send,
            "plan_travel_seconds": travel,
            "count": int(data["count"] or 1),
        })
    return rows, skipped
