"""
Mass scavenging (alpha).

One account-wide scavenge pass instead of one per village run.

The game has a screen built for exactly this - place&mode=scavenge_mass - and
it is the whole reason this module can exist. One request returns, for up to 50
villages at a time: the troops standing at home, which of the four options are
unlocked, which already have a squad out, and the world's own scavenge duration
constants. The send side matches it: scavenge_api&ajaxaction=send_squads takes a
whole array of squad_requests, each with its own village_id, and the server
accepts up to 200 of them in one POST.

So a 60-village group costs 2 page reads and 2 sends, against the ~4 requests
per village the per-village path needs (game/troopmanager.py:gather) - and, more
to the point, the per-village path only runs when that village's turn comes
round in the main loop. On an account this size a full cycle is measured in
hours, which is hours of every village's troops sitting at home between runs.
This module runs on its own clock instead.

It deliberately does NOT read per-village scavenge config. A village either
belongs to the configured group and is scavenged with the group's settings, or
it does not and its own village run keeps handling it. Two sets of rules aiming
at the same four option slots is how you get one of them quietly doing nothing.

What it still honours, because these are safety rules rather than preferences:
- villages with an incoming attack are skipped, subject to the same
  farms.gather_group_policies ladder the per-village path uses (only "always"
  lets a village keep scavenging through an incoming). The per-village
  gather_when_attacked flag is NOT read - it belongs to the other path
- troops an armed noble job has reserved for an escort stay home
- night consolidation: inside the window, one long run on the highest option,
  timed to be back by gather_night_end

Settings live in config.json under farms.mass_scavenge. It ships disabled and
has to be armed by hand, and the account-wide farms.scavenge switch stops it
along with everything else that scavenges.
"""

import datetime
import itertools
import logging
import math
import random
import time

from core.extractors import Extractor
from core.filemanager import FileManager
from game.incomings import load_groups

STATE_FILE = "cache/mass_gather.json"

# Base carry per unit. The world's own unit_carry_factor is applied on top of
# these, per village, from the mass screen.
UNIT_CARRY = {
    "spear": 25,
    "sword": 15,
    "axe": 10,
    "archer": 10,
    "light": 80,
    "marcher": 50,
    "heavy": 50,
    "knight": 100,
}

# Which runtime a village gets. A village is judged off or def by which side
# carries more of the troops it is about to scavenge with, the same call the
# in-game mass script makes.
OFF_UNITS = ("axe", "light", "marcher")

# Units picked by default: everything that can carry, minus the paladin (it
# breaks travel estimates elsewhere and there is only ever one) and minus light
# cavalry, which earns more farming than scavenging.
DEFAULT_UNITS = ["spear", "sword", "axe", "archer", "marcher", "heavy"]

# The server's own ceiling on one send_squads POST. Above it the request is
# rejected outright, so a big group is split into several sends.
MAX_SQUADS_PER_POST = 200

# The mass screen pages at 50 villages. Read at most this many pages so a
# misconfigured group can never turn into an unbounded crawl.
MAX_PAGES = 10

logger = logging.getLogger("MassGather")


def settings(config):
    """farms.mass_scavenge with its defaults filled in."""
    raw = (config.get("farms") or {}).get("mass_scavenge") or {}
    units = [u for u in (raw.get("units") or DEFAULT_UNITS) if u in UNIT_CARRY]
    options = [int(o) for o in (raw.get("options") or [1, 2, 3, 4])
               if int(o) in (1, 2, 3, 4)]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "group": raw.get("group") or "",
        # Ordered: this is also the order units are handed to the options, so
        # the first unit in the list fills the most valuable option first.
        "units": units,
        "keep_home": {u: int(n) for u, n in (raw.get("keep_home") or {}).items()
                      if u in UNIT_CARRY and int(n or 0) > 0},
        "options": sorted(set(options)),
        "runtime_off_hours": float(raw.get("runtime_off_hours", 2) or 2),
        "runtime_def_hours": float(raw.get("runtime_def_hours", 2) or 2),
        "prioritise_high_option": bool(raw.get("prioritise_high_option", True)),
        "interval_minutes": int(raw.get("interval_minutes", 120) or 120),
        "jitter_minutes": int(raw.get("jitter_minutes", 20) or 20),
        "skip_under_attack": bool(raw.get("skip_under_attack", True)),
        "night_consolidate": bool(raw.get("night_consolidate", True)),
        "night_start": int(raw.get("night_start", 23)),
        "night_end": int(raw.get("night_end", 7)),
        "night_min_hours": int(raw.get("night_min_hours", 5)),
    }


def load_state():
    return FileManager.load_json_file(STATE_FILE) or {}


def save_state(state):
    FileManager.save_json_file_atomic(state, STATE_FILE)


def due_at(state, conf, now=None):
    """When the next pass is allowed to run.

    The interval is jittered per pass rather than per tick, so the gap between
    two sends is a number that was decided once and then kept - a fresh random
    delay every tick averages out to the same time of day and puts the sends
    back on a grid."""
    now = now if now is not None else time.time()
    last = int(state.get("last_run") or 0)
    if not last:
        return now
    return last + int(state.get("next_gap") or conf["interval_minutes"] * 60)


def next_gap(conf):
    jitter = max(0, conf["jitter_minutes"]) * 60
    base = max(60, conf["interval_minutes"] * 60)
    return base + random.randint(-min(jitter, base // 2), jitter)


def resolve_group(name, groups):
    """The in-game group matching `name` (its name or its id), or None.

    Takes the group list rather than reading it, because the web process has no
    per-world data root and its FileManager would read another world's cache.
    It passes DataReader.groups_grab(); the bot passes load_groups().
    """
    if not name:
        return None
    wanted = str(name).strip().lower()
    for group in groups or []:
        if str(group.get("id")) == wanted or \
                str(group.get("name", "")).strip().lower() == wanted:
            return group
    return None


def group_villages(name, groups=None):
    """Village ids in the named in-game group, or None when it cannot be found.

    None is not an empty group: an empty group means "scavenge nothing", a
    missing one means the config points at something that no longer exists, and
    those two must not behave alike - the second is a mistake to report, not a
    quiet no-op.
    """
    group = resolve_group(name, load_groups() if groups is None else groups)
    if group is None:
        return None
    return {str(v) for v in (group.get("villages") or [])}


def group_id_for(name, groups=None):
    """The in-game id of the configured group, so the screen can be filtered
    server-side (&group=) instead of read over the whole account."""
    group = resolve_group(name, load_groups() if groups is None else groups)
    return str(group.get("id")) if group else None


def villages_under_attack():
    """Village ids with an incoming attack still on its way, from the incomings
    cache the tracker keeps. Cache-only: no requests, and a tracker that has not
    run yet simply reports nothing rather than holding the pass up."""
    under = set()
    now = time.time()
    try:
        names = FileManager.list_directory("cache/incomings", ends_with=".json")
    except FileNotFoundError:
        return under
    for name in names:
        entry = FileManager.load_json_file("cache/incomings/%s" % name) or {}
        arrival = entry.get("arrival")
        if arrival is not None and arrival <= now:
            continue
        target = entry.get("target_id")
        if target:
            under.add(str(target))
    return under


def carry_for_runtime(seconds, loot_factor, consts):
    """Largest squad carry whose run on an option of this loot factor returns
    within `seconds`.

    This is the game's duration formula turned around:
        seconds = ((carry^2 * 100 * loot_factor^2) ^ exponent + initial) * factor
    with exponent / initial / factor read from the mass screen rather than
    assumed, so a world with different constants needs no code change. (On
    nl116 factor is 0.80011, which is the world speed 1.5 raised to -0.55 -
    the same number game/troopmanager.py derives by hand.)
    """
    if not loot_factor or not seconds or seconds <= 0:
        return 0
    exponent = float(consts.get("duration_exponent") or 0.45)
    initial = float(consts.get("duration_initial_seconds") or 1800)
    factor = float(consts.get("duration_factor") or 1.0)
    inner = seconds / factor - initial
    if inner <= 0:
        return 0
    return int(math.sqrt(inner ** (1.0 / exponent) / 100.0) / loot_factor)


def night_seconds_left(conf, now=None):
    """Seconds until the night window closes, or 0 when outside it.

    Same window and same 'don't start a short run near morning' rule as the
    per-village path (game/village.py:_gather_night_consolidate), so turning a
    village over to mass scavenging does not change what its nights look like.
    """
    if not conf["night_consolidate"]:
        return 0
    start, end = conf["night_start"], conf["night_end"]
    if start == end:
        return 0
    now = now or datetime.datetime.now()
    hour = now.hour
    in_window = (start <= hour < end) if start < end else (hour >= start or hour < end)
    if not in_window:
        return 0
    end_time = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if end_time <= now:
        end_time += datetime.timedelta(days=1)
    left = int((end_time - now).total_seconds())
    if conf["night_min_hours"] > 0 and left < conf["night_min_hours"] * 3600:
        return 0
    return left


def plan_village(village, conf, options, reserved=None, night=0):
    """Squad requests for one village, or [] when it has nothing to send.

    Mirrors the in-game mass script: work out the carry that hits the wanted
    runtime, turn that into a per-option carry budget, then hand the troops out
    starting at the most valuable option. The one deliberate difference is that
    carry is measured with the village's own unit_carry_factor throughout - the
    script applies it when sizing the squad but not when filling it, which only
    agrees with itself on worlds where the factor is 1.
    """
    if not village.get("has_rally_point"):
        return []
    home = village.get("unit_counts_home") or {}
    reserved = reserved or {}
    carry_factor = float(village.get("unit_carry_factor") or 1)

    available = {}
    for unit in conf["units"]:
        count = int(home.get(unit, 0) or 0)
        count -= conf["keep_home"].get(unit, 0)
        count -= int(reserved.get(unit, 0) or 0)
        if count > 0:
            available[unit] = count
    if not available:
        return []

    carry_of = {u: UNIT_CARRY[u] * carry_factor for u in available}
    total_loot = sum(available[u] * carry_of[u] for u in available)
    if total_loot <= 0:
        return []

    off = sum(available.get(u, 0) for u in OFF_UNITS)
    hours = conf["runtime_off_hours"] if off * 2 > sum(available.values()) \
        else conf["runtime_def_hours"]

    usable = [o for o in conf["options"]
              if not (village["options"].get(str(o)) or {}).get("is_locked")
              and (village["options"].get(str(o)) or {}).get("scavenging_squad") is None]
    if not usable:
        return []

    if night:
        # One long run on the best option available, sized to be home by
        # morning. The rest of the troops stay put rather than going out on a
        # short run that would land in the middle of the night.
        usable = [max(usable)]
        seconds = night
    else:
        seconds = hours * 3600

    budget = {}
    for option in usable:
        factor = float((options.get(option) or {}).get("loot_factor") or 0)
        budget[option] = carry_for_runtime(seconds, factor, options.get(option) or {})
    total_budget = sum(budget.values())
    if total_budget <= 0:
        return []

    squads = _distribute(available, carry_of, budget, total_loot, total_budget, conf)

    requests = []
    for option in sorted(squads, reverse=True):
        units = {u: n for u, n in squads[option].items() if n > 0}
        if not units:
            continue
        requests.append({
            "village_id": village["village_id"],
            "option_id": option,
            "units": units,
            # The server sizes the squad from the unit counts; the script sends
            # a deliberately huge carry_max for the same reason, so a rounding
            # disagreement with the server cannot shrink the squad.
            "carry_max": 9999999999,
        })
    return requests


def _distribute(available, carry_of, budget, total_loot, total_budget, conf):
    """Hand the troops out over the options.

    Two cases, as in the script. With more troops than the runtime can use, the
    options are filled from the most valuable one down and whatever is left over
    stays home. With fewer, the troops are spread across the options in
    proportion to each one's budget so they all come back at about the same time
    - unless the account would rather concentrate on the best option, or the
    village has so few troops that spreading them leaves each option with a
    squad too small to be worth the trip.
    """
    remaining = dict(available)
    squads = {option: {} for option in budget}

    spread = (total_loot <= total_budget
              and not conf["prioritise_high_option"]
              and sum(available.values()) > 130)
    if spread:
        scale = total_loot / float(total_budget)
        for option in budget:
            for unit, count in available.items():
                share = scale * budget[option] * (count * carry_of[unit] / total_loot)
                squads[option][unit] = int(share / carry_of[unit])
        return squads

    for option in sorted(budget, reverse=True):
        reach = budget[option]
        for unit in conf["units"]:
            if reach <= 0:
                break
            if remaining.get(unit, 0) <= 0:
                continue
            wanted = int(reach // carry_of[unit])
            if wanted <= 0:
                continue
            take = min(wanted, remaining[unit])
            squads[option][unit] = take
            remaining[unit] -= take
            reach -= take * carry_of[unit]
    return squads


class MassGatherManager:
    """One mass scavenging pass."""

    def __init__(self, wrapper=None, config=None, reserved=None):
        self.wrapper = wrapper
        self.config = config or {}
        self.conf = settings(self.config)
        # {village_id: {unit: count}} an armed noble job wants left at home.
        self.reserved = reserved or {}

    def _read_pages(self, village_id, group_id):
        """Every village on the mass screen, page by page.

        Returns (options, villages) or (None, None) when a page could not be
        read. A half-read account is not sent: the villages that did come back
        would scavenge and the rest would look like they had nothing to send,
        which is indistinguishable from success in the log.
        """
        options, villages, seen = None, [], set()
        for page in range(MAX_PAGES):
            url = ("game.php?village=%s&screen=place&mode=scavenge_mass&page=%d"
                   % (village_id, page))
            if group_id:
                url += "&group=%s" % group_id
            data = Extractor.scavenge_mass_screen(self.wrapper.get_url(url))
            if data is None:
                logger.warning("Could not read the mass scavenge screen (page %d)", page)
                return None, None
            options = options or data["options"]
            # Paging stops on villages already seen, not on a short page. A page
            # that happens to hold exactly the page size would read as "there is
            # more", and a page index past the end does not reliably come back
            # empty - it can echo the first page, which would send every squad
            # twice.
            fresh = [v for v in data["villages"]
                     if str(v["village_id"]) not in seen]
            if not fresh:
                break
            seen.update(str(v["village_id"]) for v in fresh)
            villages.extend(fresh)
            if len(data["villages"]) < 50:
                break
            # Pages are cheap but they are still requests; space them out.
            time.sleep(random.uniform(0.8, 2.5))
        return options, villages

    def _batches(self, requests_):
        """Split the squads into POSTs, without cutting a village in half.

        Chunking on a flat index alone would put some of a village's options in
        one POST and the rest in the next, so a rejected batch would leave that
        village half-scavenging - much harder to read in the log than a whole
        village missing."""
        batches, current = [], []
        for village_id, squads in itertools.groupby(
                requests_, key=lambda r: r["village_id"]):
            squads = list(squads)
            if current and len(current) + len(squads) > MAX_SQUADS_PER_POST:
                batches.append(current)
                current = []
            current.extend(squads)
        if current:
            batches.append(current)
        return batches

    def _post_batch(self, batch):
        """One send_squads POST. True when the server took it."""
        payload = {}
        for i, squad in enumerate(batch):
            prefix = "squad_requests[%d]" % i
            payload["%s[village_id]" % prefix] = str(squad["village_id"])
            payload["%s[option_id]" % prefix] = str(squad["option_id"])
            payload["%s[use_premium]" % prefix] = "false"
            payload["%s[candidate_squad][carry_max]" % prefix] = str(squad["carry_max"])
            for unit in UNIT_CARRY:
                payload["%s[candidate_squad][unit_counts][%s]" % (prefix, unit)] = \
                    str(squad["units"].get(unit, 0))
        payload["h"] = self.wrapper.last_h
        return self.wrapper.get_api_action(
            action="send_squads",
            params={"screen": "scavenge_api"},
            data=payload,
            village_id=batch[0]["village_id"],
        ) is not None

    def _refresh_token(self, village_id):
        """Re-read a page so wrapper.last_h is current again.

        The game rotates the CSRF token on an accepted action, so every POST
        after the first in a pass carries a token the server has already spent
        and is refused. That is why the first live pass landed 200 of 207
        squads: one full POST, then a tail batch the account never saw. A page
        read costs one request and hands back a fresh token.
        """
        self.wrapper.get_url(
            "game.php?village=%s&screen=place&mode=scavenge_mass" % village_id)

    def _send(self, requests_):
        """POST the squads, in batches the server will accept."""
        sent = 0
        batches = self._batches(requests_)
        for index, batch in enumerate(batches):
            if index:
                # Fresh token for every POST after the first, and a pause so a
                # group this size does not arrive as one burst.
                time.sleep(random.uniform(2, 6))
                self._refresh_token(batch[0]["village_id"])
            if self._post_batch(batch):
                sent += len(batch)
                continue
            # One retry on a fresh token: a rejection here is almost always a
            # spent token, and the alternative is silently dropping the tail.
            logger.info("Mass scavenge send %d/%d refused, retrying with a "
                        "fresh token", index + 1, len(batches))
            time.sleep(random.uniform(2, 5))
            self._refresh_token(batch[0]["village_id"])
            if self._post_batch(batch):
                sent += len(batch)
            else:
                logger.warning(
                    "Mass scavenge send %d/%d was rejected twice - %d squad(s) "
                    "over %d village(s) not sent this pass", index + 1,
                    len(batches), len(batch),
                    len({r["village_id"] for r in batch}))
        return sent

    def run(self, force=False):
        """One pass. Returns the number of squads sent."""
        conf = self.conf
        if not conf["enabled"]:
            return 0
        # The account-wide Scavenging switch stops this too. It is the panic
        # switch - "stop scavenging" has to mean all of it, or turning it off
        # would leave sixty villages still sending runs from a module the user
        # was not looking at.
        if not (self.config.get("farms") or {}).get("scavenge", True):
            return 0
        state = load_state()
        now = time.time()
        if not force and now < due_at(state, conf, now):
            return 0

        if not conf["group"]:
            logger.warning("Mass scavenging is on but no village group is set, "
                           "nothing sent")
            return 0
        members = group_villages(conf["group"])
        if members is None:
            logger.warning(
                "farms.mass_scavenge.group is set to %r but no in-game group "
                "by that name or id is cached - nothing sent", conf["group"])
            return 0
        if not members:
            logger.info("Group %r is empty, nothing to scavenge", conf["group"])
            return 0

        anchor = sorted(members)[0]
        options, villages = self._read_pages(anchor, group_id_for(conf["group"]))
        if villages is None:
            return 0

        # The group filter is applied again here: the screen honours &group=,
        # but a stale group id would otherwise hand us the whole account.
        villages = [v for v in villages if str(v["village_id"]) in members]

        skip = villages_under_attack() if conf["skip_under_attack"] else set()
        skip &= {str(v["village_id"]) for v in villages}
        policies = (self.config.get("farms") or {}).get("gather_group_policies") or {}
        if skip and policies:
            skip = {vid for vid in skip
                    if not self._attack_override_allows(vid, policies)}

        night = night_seconds_left(conf)
        planned = []
        for village in villages:
            vid = str(village["village_id"])
            if vid in skip:
                continue
            planned.extend(plan_village(
                village, conf, options,
                reserved=self.reserved.get(vid) or self.reserved.get(int(vid)),
                night=night,
            ))

        if not planned:
            logger.info("Mass scavenge: nothing to send (%d villages checked, "
                        "%d skipped for incoming)", len(villages), len(skip))
            state.update({"last_run": int(now), "next_gap": next_gap(conf),
                          "last_sent": 0, "last_villages": len(villages),
                          "last_skipped": len(skip)})
            save_state(state)
            return 0

        sent = self._send(planned)
        logger.info("Mass scavenge: sent %d squad(s) over %d village(s)%s%s",
                    sent, len({r["village_id"] for r in planned}),
                    ", %d skipped for incoming" % len(skip) if skip else "",
                    " (night consolidation)" if night else "")
        state.update({"last_run": int(now), "next_gap": next_gap(conf),
                      "last_sent": sent, "last_villages": len(villages),
                      "last_skipped": len(skip), "last_night": bool(night)})
        save_state(state)
        return sent

    @staticmethod
    def _attack_override_allows(village_id, policies):
        """Whether a village under attack should scavenge anyway.

        Only the 'always' policy says yes. 'never' and 'pause_attacked' both
        keep the troops home, and so does having no policy at all - the
        per-village gather_when_attacked override is deliberately not read here,
        because a village handed to mass scavenging is no longer steered by its
        own scavenge settings.
        """
        for group in load_groups():
            if str(village_id) not in {str(v) for v in (group.get("villages") or [])}:
                continue
            for key in (str(group.get("id")), group.get("name")):
                if key and policies.get(key) == "always":
                    return True
        return False
