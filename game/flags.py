"""Keep the right flag on each village, from a group -> flag type plan.

A flag is not a per-village setting, which is the thing about them that catches
people out. Flags are an account-wide *inventory*: you own some number of them
of each type and level, and every one you own sits on exactly one village. A
flag given to village B is a flag taken off village A. There is no way to have
twenty villages on a resource flag while owning three of them.

The old arrangement did not know that. Each village's DefenceManager read the
flags screen on its own, saw "the best resource flag I own is level 3", and
assigned it - so twenty villages configured the same way spent the day handing
the same three flags back and forth, each one undoing the last. Nothing said so
anywhere, because it was per-village config with no account-wide view.

So flags are owned here instead, in one account-wide pass, in the same shape the
Account Manager plan uses: an ordered list of (group, flag type) rows, applied
top to bottom with the later row winning. Give [alle] the resource flag, then
give [FRONT] the defence flag, and the front villages end on defence while the
rest keep resource. A village no row matches falls back to its own
village_template.flag_type, so an account with no plan behaves as it always did.

What makes this pass different from the old one is that it counts. Demand (how
many villages want a type) is weighed against supply (how many of that type you
own), villages are served in plan order until the pool runs out, and whoever is
left over is *reported* as unmet rather than being given a flag stolen off a
village that already had it. A village whose flag is already the one it wants is
never touched, which is what stops the shuffling.

ALPHA. The flag screen's own reading has never run on a live account here
(flags.manage has been off since the fork), so the parser below is written to
survive a shape it did not expect: whatever setFlagCounts sends is kept raw in
the state cache, and the dashboard shows it when the parse comes up empty.
Nothing is assigned unless you ask for it - see `run`.
"""

import json
import logging
import os
import re
import time
from datetime import date

from core.filemanager import FileManager
# The plan file is written by both processes - the dashboard edits it and asks
# for a run, the bot clears the request once it has served it - so it goes under
# the same cross-process lock the attack queue and the AM plan use.
from game.attack_scheduler import _Lock
from game.incomings import load_groups

logger = logging.getLogger("Flags")

PLAN_CACHE = "cache/flag_plan.json"
STATE_CACHE = "cache/flag_state.json"

# TribalWars flag type ids. Stable across worlds, and the same ids the
# per-village flag_type dropdown has always used, so an existing config keeps
# meaning what it meant. The effect text is what the game's own flag screen
# says a level-1 flag does; higher levels scale it up.
FLAG_TYPES = {
    1: ("Resource production", "More wood, clay and iron produced in this village."),
    2: ("Recruitment speed", "Barracks, stable and workshop build units faster."),
    3: ("Attack strength", "Troops attacking FROM this village hit harder."),
    4: ("Defence strength", "Troops defending IN this village hold out longer."),
    5: ("Luck", "Raises the luck of attacks sent from this village."),
    6: ("Population", "More farm space, so the village supports more troops."),
    7: ("Coin cost", "Minting coins in this village costs fewer resources."),
    8: ("Haul", "Troops farming from this village carry more loot home."),
}
FLAG_TYPE_ORDER = tuple(sorted(FLAG_TYPES))

# How long a cached per-village flag reading stands before it is read again.
# A flag only moves when this pass moves it, so the cache is authoritative
# almost all the time; the re-read exists to catch a flag moved by hand in the
# game, which is a thing that happens on the scale of days, not minutes.
READING_TTL = 12 * 3600
# Village flag screens to read in one pass. Each is one request, and a first
# run on a 50-village account would otherwise go out as a 50-request burst.
DEFAULT_MAX_READS = 15

_RE_FLAG_COUNTS = re.compile(r"FlagsScreen\.setFlagCounts\((.+?)\);")
# The flag standing on a village is rendered as its own image inside the
# current-flag block, named <type>_<level>.png. Only that pairing is looked for:
# the block's surrounding markup (the name in a <p>, the closing tags) differs
# between worlds and skins, and an exact-shape match on it reads a village that
# HAS a flag as one that has none - which is the reading that makes the pass
# assign a flag the village is already carrying.
_RE_CURRENT_FLAG_BLOCK = re.compile(r'(?s)<div id="current_flag"(.{0,2000})')
_RE_FLAG_IMAGE = re.compile(r'/(\d+)_(\d+)\.png')
_NO_FLAG_MARKER = '<div id="current_flag" style="margin-top: 10px; display: none">'
_COOLDOWN_MARKER = '<span class="timer cooldown">'


def flag_name(flag_type):
    """Human name for a flag type id, including ones the game adds later."""
    entry = FLAG_TYPES.get(int(flag_type or 0))
    return entry[0] if entry else "Flag type %s" % flag_type


# -- the plan --------------------------------------------------------------

def _plan_path(path=None):
    """Where the plan lives. The dashboard has to pass one: its process has no
    per-world data root, so FileManager there would answer with the default
    world's file whichever world is being looked at."""
    return path or FileManager.get_path(PLAN_CACHE)


def _normalize(raw):
    """Always the same shape, whatever is (or is not) in the file."""
    raw = raw if isinstance(raw, dict) else {}
    rows = raw.get("rows")
    plan = {"rows": [r for r in rows if isinstance(r, dict)]
            if isinstance(rows, list) else []}
    plan["run_now"] = bool(raw.get("run_now"))
    plan["refresh"] = bool(raw.get("refresh"))
    return plan


def _read_plan(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def load_plan(path=None):
    """The saved group -> flag type plan: {"rows": [{group_id, group_name,
    flag_type}], "run_now": bool, "refresh": bool}.

    Reads are lock-free, because writes are atomic."""
    return _normalize(_read_plan(_plan_path(path)))


def update_plan(mutator, path=None):
    """Read -> mutate -> write the plan under the cross-process lock, so the
    dashboard asking for a run cannot be lost by the bot writing back a cleared
    request it read a moment earlier (or the other way round)."""
    target = _plan_path(path)
    with _Lock(target):
        plan = _normalize(_read_plan(target))
        mutator(plan)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = "%s.tmp.%d" % (target, os.getpid())
        with open(tmp, "w") as handle:
            json.dump(plan, handle, indent=2)
        os.replace(tmp, target)  # atomic, so a reader never sees half a file
    return plan


def load_state(path=None):
    if path:
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}
    return FileManager.load_json_file(STATE_CACHE) or {}


def save_state(state):
    FileManager.save_json_file(state, STATE_CACHE)


# -- reading the game ------------------------------------------------------

def _count(raw):
    """One level's count out of setFlagCounts, whatever shape it arrives in.

    The old per-village reader iterated this value, so on the world it was
    written against it was a list; the shape is not documented anywhere and has
    never been seen on this account. A bare number, a list and a dict are all
    read rather than assumed, and a list is taken at its largest element rather
    than summed - a two-element [free, owned] pair must not read as their total.
    """
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, dict):
        raw = list(raw.values())
    if isinstance(raw, (list, tuple)):
        counts = [_count(item) for item in raw]
        return max(counts) if counts else 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_inventory(page):
    """{flag_type: {level: count}} out of the flag screen, plus the raw JSON.

    The raw text is carried along so the dashboard can show what the game
    actually sent when the parse comes up empty - which is the only way to fix
    a shape guess without switching the whole thing on against a live account.
    """
    match = _RE_FLAG_COUNTS.search(page)
    if not match:
        return {}, None
    try:
        raw = json.loads(match.group(1))
    except ValueError:
        return {}, match.group(1)[:2000]
    inventory = {}
    if not isinstance(raw, dict):
        return {}, match.group(1)[:2000]
    for flag_type, levels in raw.items():
        try:
            type_id = int(flag_type)
        except (TypeError, ValueError):
            continue
        if not isinstance(levels, dict):
            continue
        by_level = {}
        for level, amount in levels.items():
            try:
                level_id = int(level)
            except (TypeError, ValueError):
                continue
            count = _count(amount)
            if count > 0:
                by_level[level_id] = count
        if by_level:
            inventory[type_id] = by_level
    return inventory, match.group(1)[:2000]


def parse_current_flag(page):
    """[type, level] of the flag on the village whose screen this is, or None.

    None means "no flag on this village", which the screen says by rendering
    the current-flag block hidden rather than by leaving it out.
    """
    if _NO_FLAG_MARKER in page:
        return None
    block = _RE_CURRENT_FLAG_BLOCK.search(page)
    if not block:
        return None
    image = _RE_FLAG_IMAGE.search(block.group(1))
    if not image:
        return None
    return [int(image.group(1)), int(image.group(2))]


def read_screen(wrapper, village_id):
    """One village's flag screen: {inventory, raw, current, cooldown}.

    The inventory is account-wide, so any village's screen answers for the whole
    account - reading a second one only tells you a second current flag.
    """
    result = wrapper.get_url("game.php?village=%s&screen=flags" % village_id)
    if result is None:
        return None
    page = result.text
    inventory, raw = parse_inventory(page)
    return {
        "inventory": inventory,
        "raw": raw,
        "current": parse_current_flag(page),
        "cooldown": _COOLDOWN_MARKER in page,
    }


def assign_flag(wrapper, village_id, flag_type, level):
    """Put a flag on a village. The one it was carrying returns to the pool."""
    return wrapper.get_api_action(
        village_id,
        action="assign_flag",
        params={"screen": "flags", "h": wrapper.last_h},
        data={"flag_type": str(flag_type), "level": str(level),
              "village_id": str(village_id)},
    )


def upgrade_flag(wrapper, village_id, flag_type, level):
    """Combine three flags of one type and level into one of the next."""
    return wrapper.get_api_action(
        village_id,
        action="upgrade_flag",
        params={"screen": "flags", "h": wrapper.last_h},
        data={"flag_type": str(flag_type), "from_level": str(level)},
    )


# -- what each village should be carrying ----------------------------------

def _fallback_type(config, village_id):
    """The village's own flag_type setting, which is what a village no plan row
    matches keeps carrying. Every managed village has one materialised from
    village_template at the moment it was added, so this is almost never the
    template read - but an older config predates the key."""
    entry = ((config or {}).get("villages") or {}).get(str(village_id)) or {}
    raw = entry.get("flag_type",
                    ((config or {}).get("village_template") or {}).get("flag_type", 1))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def resolve_desired(rows, groups, village_ids, config):
    """{village_id: {flag_type, source, group, priority}} for every village.

    Rows are applied top to bottom and the later row wins, which is how the
    usual layout is built: give every village the resource flag, then give
    [OFF] the attack flag, and the off villages end on attack while the rest
    keep resource. `priority` is the index of the row that decided it, so when
    the pool runs out the more specific row is the one that gets served (see
    `allocate`). A village no row matches keeps its own setting at priority -1.

    A row may name its group by id or by name; the name is matched
    case-insensitively, so a group renamed in game breaks the row loudly (it
    stops matching) rather than quietly pointing at the wrong villages.
    """
    desired = {}
    for vid in village_ids:
        desired[str(vid)] = {
            "flag_type": _fallback_type(config, vid),
            "source": "village", "group": None, "priority": -1,
        }
    by_id, by_name = {}, {}
    for group in groups or []:
        members = [str(v) for v in (group.get("villages") or [])]
        by_id[str(group.get("id", ""))] = (group, members)
        by_name[str(group.get("name", "")).lower()] = (group, members)

    for index, row in enumerate(rows or []):
        try:
            flag_type = int(row.get("flag_type") or 0)
        except (TypeError, ValueError):
            continue
        group_id = str(row.get("group_id", ""))
        # Group id 0 is the game's built-in "all villages" pseudo group. The
        # group cache deliberately leaves it out (it has no membership list of
        # its own), so it is resolved here against everything managed.
        if group_id in ("0", "all"):
            group, members = {"id": "0", "name": "All villages"}, list(desired)
        else:
            found = by_id.get(group_id) or \
                by_name.get(str(row.get("group_name", "")).lower())
            if not found:
                continue
            group, members = found
        for vid in members:
            if vid not in desired:
                continue
            desired[vid] = {
                "flag_type": flag_type, "source": "group",
                "group": group.get("name") or group.get("id"),
                "priority": index,
            }
    return desired


def allocate(desired, inventory, readings):
    """Decide who gets a flag this pass, and who has to wait.

    Supply is what you own minus what is already standing on a village
    (`readings`), so a flag that is doing its job is never counted as spare and
    never taken. Villages already carrying the type they want are left
    untouched - that, not the counting, is what stops the shuffling.

    Everyone else is served best-level-first in priority order, and a village
    that gets a new flag hands its old one back to the pool, because that is
    what the game does. Whoever is left over comes back under "unmet" for the
    dashboard to explain, rather than being served a flag stolen from a village
    that already had it.

    Returns (moves, unmet, free) where moves is [{village_id, flag_type, level,
    from}], unmet is [{village_id, flag_type}] and free is the pool left over.
    """
    free = {int(t): dict(levels) for t, levels in (inventory or {}).items()}
    for vid, reading in (readings or {}).items():
        flag = (reading or {}).get("flag")
        if not flag:
            continue
        flag_type, level = int(flag[0]), int(flag[1])
        if free.get(flag_type, {}).get(level):
            free[flag_type][level] -= 1

    def _current(vid):
        flag = ((readings or {}).get(str(vid)) or {}).get("flag")
        return (int(flag[0]), int(flag[1])) if flag else None

    # Most specific row first; a plain village setting last. Ties break on
    # village id so a pass that changes nothing produces the same plan twice.
    order = sorted(desired.items(),
                   key=lambda kv: (-kv[1].get("priority", -1), str(kv[0])))
    moves, unmet = [], []
    for vid, want in order:
        flag_type = int(want.get("flag_type") or 0)
        if flag_type <= 0:
            continue  # 0 = never assign a flag to this village
        current = _current(vid)
        if current and current[0] == flag_type:
            continue  # already carrying the right type, at whatever level
        levels = free.get(flag_type) or {}
        best = max((lvl for lvl, count in levels.items() if count > 0),
                   default=None)
        if best is None:
            unmet.append({"village_id": str(vid), "flag_type": flag_type})
            continue
        free[flag_type][best] -= 1
        if current:  # the flag it was carrying goes back in the drawer
            free.setdefault(current[0], {})
            free[current[0]][current[1]] = free[current[0]].get(current[1], 0) + 1
        moves.append({"village_id": str(vid), "flag_type": flag_type,
                      "level": best, "from": list(current) if current else None})
    return moves, unmet, free


def demand(desired):
    """{flag_type: village count} - what the plan asks the inventory for."""
    wanted = {}
    for want in (desired or {}).values():
        flag_type = int(want.get("flag_type") or 0)
        if flag_type > 0:
            wanted[flag_type] = wanted.get(flag_type, 0) + 1
    return wanted


# -- the pass --------------------------------------------------------------

def _api_error(result):
    """What went wrong, out of whatever get_api_action handed back.

    The action answers with parsed JSON on a good day, a raw Response when the
    body was not JSON, and None when the request itself failed. Only the first
    can carry the game's own refusal, so the other two are named for what they
    are rather than reported as a success.
    """
    if result is None:
        return "the request failed"
    if isinstance(result, dict):
        error = result.get("error")
        response = result.get("response")
        if not error and isinstance(response, dict):
            error = response.get("error")
        return str(error) if error else None
    return None  # a non-JSON 200 is the game's normal answer on some worlds


def refresh(wrapper, village_ids, max_reads=None, state=None):
    """Read the account's flag inventory, and re-read stale village flags.

    The inventory is account-wide, so the first village's screen answers for
    the whole account and every extra read buys exactly one more "which flag is
    on this village". Those readings are cached because a flag only moves when
    this pass moves it - the re-read is there to notice a flag moved by hand -
    so a pass costs one request on a settled account and is capped at
    `max_reads` on a cold one.
    """
    village_ids = [str(v) for v in village_ids or []]
    if not village_ids:
        return state or load_state()
    state = dict(state if state is not None else load_state())
    readings = dict(state.get("readings") or {})
    now = int(time.time())
    budget = int(max_reads or DEFAULT_MAX_READS)

    stale = [vid for vid in village_ids
             if now - int((readings.get(vid) or {}).get("ts", 0)) > READING_TTL]
    # Always read one village even when nothing is stale: that is the inventory
    # read, and the oldest reading is the one worth spending it on.
    if not stale:
        stale = [min(village_ids,
                     key=lambda v: int((readings.get(v) or {}).get("ts", 0)))]

    read, cooldown = 0, False
    for vid in stale:
        if read >= budget:
            break
        screen = read_screen(wrapper, vid)
        read += 1
        if screen is None:
            logger.warning("Could not read the flag screen for village %s", vid)
            continue
        readings[vid] = {"flag": screen["current"], "ts": now}
        cooldown = cooldown or screen["cooldown"]
        if screen["inventory"]:
            state["inventory"] = {str(t): {str(l): c for l, c in levels.items()}
                                  for t, levels in screen["inventory"].items()}
            state["raw"] = None
        elif screen["raw"] is not None:
            # Parsed to nothing but the screen did send something: keep it, so
            # the dashboard can show what shape it was instead of saying "no
            # flags" about an account that has them.
            state["raw"] = screen["raw"]

    state["readings"] = readings
    state["read_when"] = now
    state["cooldown"] = cooldown
    state["unread"] = [vid for vid in village_ids if vid not in readings]
    save_state(state)
    return state


def _inventory(state):
    """The saved inventory back as {int type: {int level: count}}."""
    inventory = {}
    for flag_type, levels in (state.get("inventory") or {}).items():
        try:
            type_id = int(flag_type)
        except (TypeError, ValueError):
            continue
        by_level = {}
        for level, count in (levels or {}).items():
            try:
                by_level[int(level)] = int(count)
            except (TypeError, ValueError):
                continue
        inventory[type_id] = by_level
    return inventory


def _auto_upgrade(wrapper, village_id, state):
    """Combine every three flags of one type and level into one of the next.

    Off unless flags.auto_upgrade is set, because it consumes the user's flags:
    three level-1 defence flags become one level-2, and there is no way back.
    Only the free pool is combined - a flag standing on a village is not one of
    the three.
    """
    upgraded = 0
    inventory = _inventory(state)
    held = {}
    for reading in (state.get("readings") or {}).values():
        flag = (reading or {}).get("flag")
        if flag:
            held[(int(flag[0]), int(flag[1]))] = held.get(
                (int(flag[0]), int(flag[1])), 0) + 1
    for flag_type, levels in sorted(inventory.items()):
        for level, count in sorted(levels.items()):
            spare = count - held.get((flag_type, level), 0)
            while spare >= 3:
                result = upgrade_flag(wrapper, village_id, flag_type, level)
                error = _api_error(result)
                if error:
                    logger.warning("Could not upgrade %s level %d: %s",
                                   flag_name(flag_type), level, error)
                    spare = 0
                    break
                logger.info("Upgraded three %s flags from level %d to %d",
                            flag_name(flag_type), level, level + 1)
                upgraded += 1
                spare -= 3
    return upgraded


def apply_plan(wrapper, village_id, config, village_ids, source="bot"):
    """Read, decide, assign - and write down what happened for the dashboard.

    Returns the saved state. Every move is a value in `last_result`, error or
    not: one village the game refuses must not stop the rest of the plan.
    """
    settings = (config or {}).get("flags", {}) or {}
    plan = load_plan()
    state = refresh(wrapper, village_ids, settings.get("max_reads_per_run"))

    upgraded = 0
    if settings.get("auto_upgrade", False):
        upgraded = _auto_upgrade(wrapper, village_id, state)
        if upgraded:
            state = refresh(wrapper, village_ids,
                            settings.get("max_reads_per_run"), state=state)

    desired = resolve_desired(plan.get("rows"), load_groups(), village_ids, config)
    moves, unmet, free = allocate(desired, _inventory(state),
                                  state.get("readings") or {})

    results = []
    if moves and state.get("cooldown"):
        # The game puts a timer on flag changes; a POST inside it is refused.
        # Saying so once beats a row of identical failures.
        logger.info("Flag changes are on cooldown - %d move(s) wait for the "
                    "next pass", len(moves))
        results = [{"village_id": move["village_id"], "flag_type": move["flag_type"],
                    "level": move["level"], "ok": False,
                    "error": "flag change is on cooldown"} for move in moves]
        moves = []

    readings = dict(state.get("readings") or {})
    for move in moves:
        error = _api_error(assign_flag(wrapper, move["village_id"],
                                       move["flag_type"], move["level"]))
        if not error:
            readings[move["village_id"]] = {
                "flag": [move["flag_type"], move["level"]], "ts": int(time.time())}
            logger.info("Gave village %s the %s flag (level %d)",
                        move["village_id"], flag_name(move["flag_type"]),
                        move["level"])
        else:
            logger.warning("Could not give village %s the %s flag: %s",
                           move["village_id"], flag_name(move["flag_type"]), error)
        results.append({"village_id": move["village_id"],
                        "flag_type": move["flag_type"], "level": move["level"],
                        "from": move["from"], "ok": not error, "error": error})

    state["readings"] = readings
    state["last_result"] = results
    state["last_result_when"] = int(time.time())
    state["last_result_source"] = "from the dashboard" if source == "dashboard" \
        else "by the bot"
    state["unmet"] = unmet
    state["upgraded"] = upgraded
    state["free"] = {str(t): {str(l): c for l, c in levels.items() if c > 0}
                     for t, levels in free.items()}
    state["desired"] = desired
    save_state(state)
    return state


def run(wrapper, village_id, config, active_hours=True):
    """Serve what the dashboard asked for, and re-apply the plan once a day.

    Nothing here turns itself on. A refresh is read-only, so it is served
    whenever it is asked - that is how you look at your flags before handing
    them over. Assigning needs flags.manage, and the unattended daily pass
    needs flags.auto_assign on top of it, so the normal way to use this is to
    build a plan and press Apply.
    """
    if not village_id:
        return
    village_ids = [str(v) for v in ((config or {}).get("villages") or {})]
    if not village_ids:
        return
    settings = (config or {}).get("flags", {}) or {}
    plan = load_plan()

    # Requests are cleared before they are served, never after: a request that
    # somehow crashes the pass has to be a one-off, not something the bot
    # retries every cycle for the rest of the day.
    if plan.get("refresh"):
        update_plan(lambda p: p.update({"refresh": False}))
        refresh(wrapper, village_ids, settings.get("max_reads_per_run"))

    if plan.get("run_now"):
        update_plan(lambda p: p.update({"run_now": False}))
        if not settings.get("manage", False):
            logger.info("Apply was asked for but flags.manage is off - nothing "
                        "was assigned")
        else:
            logger.info("Applying the flag plan (asked from the dashboard)")
            apply_plan(wrapper, village_id, config, village_ids,
                       source="dashboard")
        return

    if not settings.get("manage", False) or not settings.get("auto_assign", False):
        return
    if not active_hours:
        return
    today = date.today().isoformat()
    state = load_state()
    if state.get("last_run") == today:
        return
    logger.info("Applying the flag plan for %s", today)
    apply_plan(wrapper, village_id, config, village_ids, source="bot")
    # Written after the pass, so a crash midway retries next cycle rather than
    # skipping the day.
    state = load_state()
    state["last_run"] = today
    state["last_run_ts"] = int(time.time())
    save_state(state)
