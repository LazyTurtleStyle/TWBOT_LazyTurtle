"""
Keeps villages up to date from the account-wide overview pages.

The main loop opens every village in turn: its overview (three times over the
course of a visit), its troop page and its quest popup. That is the right thing
for a village the bot farms from or builds in. It is five requests of pure
bookkeeping for one where the Account Manager builds and recruits, the mass
pass scavenges and nothing is farmed - and on a grown account that is nearly
every village. Measured on a live world of 73: 79% of all requests, one pass
every 58 minutes, with nothing done in any of them.

The game already prints what those visits collect, for every village at once:
stock, storage, merchants and farm space on the production overview, troops on
the units overview the main loop caches anyway. So a village with nothing
per-village switched on (Village.visit_reason) is refreshed from those two
pages instead, and is only opened now and then - a few villages per pass, each
once per bot.light_full_visit_hours - for what no overview carries: quest
rewards, production rates, a renamed village.

Bots are detected by what they request, so the cheapest request is the one not
made. Nothing here is on unless bot.light_villages is.
"""
import logging
import time

from core.extractors import Extractor
from core.filemanager import FileManager

SNAPSHOT = "cache/managed/%s.json"
TROOPS = "cache/troops_moving.json"
# A troop reading older than this says little about who is home now; the
# snapshot then keeps the counts it had rather than taking stale ones as new.
TROOPS_MAX_AGE = 1800

logger = logging.getLogger("LightPass")
# Reasons for opening a village that are not work done there. A pass in which
# nothing else was opened had nothing to do, and the next one can wait.
NOT_WORK = ("light villages off", "periodic visit", "never visited",
            "not managed", "not on the overview")


def settings(config):
    """The three bot.light_* settings, with their defaults."""
    bot = (config or {}).get("bot") or {}

    def number(key, default):
        try:
            return max(0.0, float(bot.get(key, default)))
        except (TypeError, ValueError):
            return float(default)

    return {
        "enabled": bool(bot.get("light_villages", False)),
        "full_visit_hours": number("light_full_visit_hours", 24),
        "full_visits_per_pass": int(number("light_full_visits_per_pass", 3)),
        "cycle_minutes": number("light_cycle_minutes", 60),
    }


def read_production(wrapper, village_id):
    """Every village's row of the production overview, {} when it cannot be read.

    page=-1 asks for all villages at once: the overview otherwise pages at
    whatever the player set (50 by default), and a village on page two would
    silently go back to being visited every cycle.
    """
    res = wrapper.get_url(
        "game.php?village=%s&screen=overview_villages&mode=prod&group=0&page=-1"
        % village_id)
    if not res:
        return {}
    return Extractor.production_overview(res)


def village_troops(village_id, now=None):
    """(home, total) for one village from the cached units overview, or
    (None, None) when there is no reading recent enough to stand in for a
    look at the village itself."""
    cache = FileManager.load_json_file(TROOPS) or {}
    located = (cache.get("by_village") or {}).get(str(village_id))
    read_at = int(cache.get("complete_when") or cache.get("when") or 0)
    if not located or (now or time.time()) - read_at > TROOPS_MAX_AGE:
        return None, None
    own = located.get("own") or {}
    total = {
        unit: int(own.get(unit) or 0)
        + int((located.get("elsewhere") or {}).get(unit) or 0)
        + int((located.get("moving") or {}).get(unit) or 0)
        for unit in own
    }
    # The same shape a visit writes: only the units that are there, as the
    # strings the units page gives.
    home = {unit: str(count) for unit, count in own.items() if int(count or 0) > 0}
    return home, total


def refresh_snapshot(village_id, row, now=None):
    """Bring cache/managed/<id>.json up to date without opening the village.

    Returns the snapshot, or None when the village has none yet - it has to be
    visited once before it can be left alone. Everything the overviews do not
    carry (building levels, production rates, the scavenge state) stays as the
    last visit left it.
    """
    now = int(now or time.time())
    path = SNAPSHOT % village_id
    snapshot = FileManager.load_json_file(path)
    if not snapshot:
        return None
    # A snapshot from before this module existed was written by a visit.
    snapshot.setdefault("last_visit", int(snapshot.get("last_run") or 0))
    resources = dict(row["resources"])
    resources["pop"] = row["pop_max"] - row["pop_used"]
    snapshot["resources"] = resources
    snapshot["storage_max"] = row["storage_max"]
    snapshot["pop_used"] = row["pop_used"]
    snapshot["pop_max"] = row["pop_max"]
    snapshot["under_attack"] = row["under_attack"]
    public = dict(snapshot.get("public") or {})
    public["points"] = row["points"]
    if row.get("name"):
        snapshot["name"] = row["name"]
        public["name"] = row["name"]
    if row.get("location"):
        public["location"] = row["location"]
    snapshot["public"] = public
    home, total = village_troops(village_id, now)
    if home is not None:
        snapshot["available_troops"] = home
        snapshot["troops"] = total
    snapshot["last_run"] = now
    FileManager.save_json_file_atomic(snapshot, path)
    return snapshot


def due_for_visit(snapshots, hours, limit, now=None):
    """The villages whose turn it is to be opened for real this pass.

    Longest-unvisited first and only a few per pass, so the visits are spread
    over the day instead of arriving as one long burst of every village at
    once - which is exactly the traffic this module exists to stop making.
    """
    if hours <= 0 or limit <= 0:
        return set()
    now = now or time.time()
    overdue = sorted(
        (int(snap.get("last_visit") or snap.get("last_run") or 0), vid)
        for vid, snap in snapshots.items()
        if now - int(snap.get("last_visit") or snap.get("last_run") or 0) >= hours * 3600
    )
    return {vid for _, vid in overdue[:limit]}
