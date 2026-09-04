"""Play and track the rotating in-game events.

TribalWars runs a themed event most weeks: a screen where an energy bar that
refills on its own is spent on some dressed-up gamble, paying an event currency
you spend in an event shop. The energy is the whole game - it refills at a fixed
rate up to a cap, so every hour the bar sits full is an action thrown away, and
"playing well" is mostly "not being asleep". That is a bot's job, not a
player's.

An event is noticed rather than looked for: every game page carries a link to
the running event in the header menu, so the bot reads it off a page it already
loads and stays silent on the weeks there is no event. When the link disappears
the event is over: it stops being played, is marked finished, and what it paid
out stays on file as history.

Each event needs its own driver, because only the dressing is shared. The one
here is the horse race (Kampioenschap van de Paardenheren), where a cheer costs
one fodder and pays its value into both the team's distance and your trophies.
Its four options are a straight expected-value question:

    option 1: 50 flat
    option 2: 10, plus a 25% chance at a jackpot
    option 3: 17, plus a 10% chance at a jackpot
    option 4: 25, plus a 5%  chance at a jackpot

and the jackpots are progressive - option 4's was seen to climb from 575 to
1400 within an hour on nl116, which moves it from the worst option (EV 53.75) to
by far the best (EV 95). So the driver does not hardcode a favourite: it reads
the live jackpots every cycle and spends on whichever option is worth most right
then. That is the part a human cannot do well, because it means re-checking the
board before every single click.
"""

import html as html_module
import json
import logging
import os
import re
import time
from datetime import datetime

from core.filemanager import FileManager

logger = logging.getLogger("Events")

EVENTS_DIR = "cache/events"
# Kept per event so the dashboard can show what the bot did, without the file
# growing without bound on a week-long event.
MAX_LOG = 300
# A cycle should never fire more than a full bar's worth: anything beyond that
# means something is wrong with the energy accounting, and the loop stops rather
# than hammering the endpoint.
MAX_ACTIONS_PER_CYCLE = 20

# The header menu of every game page carries the running event.
_RE_EVENT_LINK = re.compile(
    r'<a href="[^"]*screen=(event_[a-z_]+)[^"]*"[^>]*title="([^"]*)"[^>]*>\s*'
    r'<img[^>]*menu-event-icon', re.S)
# "Event loopt af op 07.09. om 14:00" / "... on 07.09. at 14:00".
_RE_ENDS = re.compile(r'(\d{1,2})[./-](\d{1,2})[.]?\D{1,8}(\d{1,2}):(\d{2})')
_RE_OPTION_CHANCE = re.compile(r'event-option-chance[^>]*>.*?<span>(\d+)%</span>', re.S)
_RE_OPTION_JACKPOT = re.compile(r'id="event-option-jackpot-\d+">(.*?)</span>', re.S)
_RE_OPTION_REWARD = re.compile(
    r'event-option-reward">.*?<span class="reward">(.*?)</span>', re.S)
_RE_DESCRIPTION = re.compile(r'class="event-description">(.*?)</div>', re.S)
# Who "you" are, so the ranking tables can point at your own row: the rankings
# name every player but never say which one is the reader.
_RE_PLAYER = re.compile(r'"player":\{"id":(\d+),"name":"([^"]*)"')


def _num(raw):
    """A number as the game writes it ("1<span>.</span>400") as an int."""
    digits = re.sub(r"[^\d]", "", html_module.unescape(
        re.sub(r"<[^>]+>", "", raw or "")))
    return int(digits) if digits else 0


def _ajax_headers(wrapper):
    """Headers that make the game answer with JSON instead of a whole page."""
    headers = dict(wrapper.headers)
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["TribalWars-Ajax"] = "1"
    return headers


# -- state on disk ---------------------------------------------------------

def state_path(screen):
    return os.path.join(EVENTS_DIR, "%s.json" % re.sub(r"[^a-z_]", "", screen))


def load_state(screen):
    return FileManager.load_json_file(state_path(screen)) or {}


def save_state(state):
    FileManager.create_directories([EVENTS_DIR])
    FileManager.save_json_file(state, state_path(state["screen"]))


def list_states():
    """Every event the bot has seen, newest first."""
    out = []
    directory = FileManager.get_path(EVENTS_DIR)
    if not os.path.isdir(directory):
        return out
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        data = FileManager.load_json_file(os.path.join(EVENTS_DIR, name))
        if data:
            out.append(data)
    return sorted(out, key=lambda s: s.get("last_seen") or 0, reverse=True)


# -- detection -------------------------------------------------------------

def detect(page):
    """The running event as (screen, name), or (None, None) on a quiet week."""
    if not page:
        return None, None
    found = _RE_EVENT_LINK.search(page)
    if not found:
        return None, None
    return found.group(1), html_module.unescape(found.group(2)).strip()


# -- the horse race --------------------------------------------------------

def _horse_read_page(wrapper, village_id, screen):
    """The parts of the event that only change when the event does: what the
    options pay, and when it ends. Read once a day, not every cycle."""
    res = wrapper.get_url(f"game.php?village={village_id}&screen={screen}")
    if res is None or not getattr(res, "text", ""):
        return None
    page = res.text
    options = []
    for chunk in page.split('<div class="event-option option-')[1:]:
        oid = chunk.split('"', 1)[0]
        if not oid.isdigit():
            continue
        chance = _RE_OPTION_CHANCE.search(chunk)
        jackpot = _RE_OPTION_JACKPOT.search(chunk)
        reward = _RE_OPTION_REWARD.search(chunk)
        options.append({
            "id": oid,
            "base": _num(reward.group(1)) if reward else 0,
            "chance": int(chance.group(1)) if chance else 0,
            "jackpot": _num(jackpot.group(1)) if jackpot else 0,
        })
    description = _RE_DESCRIPTION.search(page)
    ends_text = ""
    if description:
        text = re.sub(r"\s+", " ", html_module.unescape(
            re.sub(r"<[^>]+>", " ", description.group(1)))).strip()
        for line in text.split("."):
            if re.search(r"\d{1,2}:\d{2}", line):
                ends_text = text
                break
    me = _RE_PLAYER.search(page)
    return {"options": options, "ends_text": ends_text,
            "ends_ts": _parse_ends(ends_text),
            "player_id": int(me.group(1)) if me else None,
            "player_name": html_module.unescape(me.group(2)) if me else ""}


def _parse_ends(text):
    """The end moment as a timestamp, or None when the wording is unfamiliar.

    Only ever used for display, so a locale this does not understand costs a
    countdown, not the feature.
    """
    found = _RE_ENDS.search(text or "")
    if not found:
        return None
    day, month, hour, minute = (int(x) for x in found.groups())
    now = datetime.now()
    for year in (now.year, now.year + 1):
        try:
            when = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if when > now:
            return int(when.timestamp())
    return None


def _horse_poll(wrapper, village_id, screen):
    """Energy, currency, rankings and the live jackpots, in one small request."""
    res = wrapper.get_url(f"game.php?village={village_id}&screen={screen}&ajax=poll",
                          headers=_ajax_headers(wrapper))
    if res is None:
        return None
    try:
        return (res.json() or {}).get("response") or None
    except ValueError:
        logger.debug("Event poll did not answer with JSON")
        return None


def energy_rate(energies, key="fodder"):
    """Seconds per unit of energy, so the page can tick the bar between passes."""
    try:
        return int((energies or {})[key]["recharge_seconds"])
    except (KeyError, TypeError, ValueError):
        return 0


def energy_now(energies, key="fodder"):
    """Energy right now, projected from the snapshot the game hands out.

    The game reports a value and the moment it was true, plus the refill rate;
    it never reports "now". Capped, because a full bar stops refilling - which
    is the one thing this whole module exists to avoid.
    """
    spec = (energies or {}).get(key) or {}
    try:
        value = float(spec["snapshot_value"])
        rate = float(spec["recharge_seconds"])
        top = float(spec["max_value"])
    except (KeyError, TypeError, ValueError):
        return 0.0, 0.0
    grown = value + (time.time() - float(spec["snapshot_time"])) / rate
    return min(grown, top), top


def option_values(options, jackpots):
    """Each option with what it is worth per unit of energy, best first.

    Worth = the guaranteed part plus the jackpot times its chance. The jackpots
    move, so this is a live question, not a table to memorise.
    """
    rated = []
    for option in options or []:
        jackpot = int((jackpots or {}).get(str(option["id"]))
                      or option.get("jackpot") or 0)
        chance = float(option.get("chance") or 0) / 100.0
        rated.append(dict(option, jackpot=jackpot,
                          value=option.get("base", 0) + chance * jackpot))
    return sorted(rated, key=lambda o: o["value"], reverse=True)


def _horse_play(wrapper, village_id, screen, state, poll, choice="auto"):
    """Spend the bar down, on the best option available at each click."""
    energy, _top = energy_now(poll.get("player_energies"))
    jackpots = ((poll.get("player_group") or {}).get("race") or {}).get("jackpots")
    done = []
    while int(energy) >= 1 and len(done) < MAX_ACTIONS_PER_CYCLE:
        rated = option_values(state.get("options"), jackpots)
        if not rated:
            break
        pick = rated[0]
        if str(choice) != "auto":
            wanted = [o for o in rated if str(o["id"]) == str(choice)]
            if wanted:
                pick = wanted[0]
        result = wrapper.get_api_action(
            village_id=village_id, action="progress",
            params={"screen": screen},
            data={"option_id": pick["id"], "doubler": "false"},
        )
        if not isinstance(result, dict):
            logger.warning("Event action did not go through, stopping this pass")
            break
        response = result.get("response") or result
        reward = int(response.get("reward") or 0)
        jackpot_hit = bool(response.get("jackpot_message"))
        done.append({
            "ts": int(time.time()),
            "option": pick["id"],
            "reward": reward,
            "jackpot": jackpot_hit,
            "expected": round(pick["value"], 2),
            "currency": response.get("currency"),
        })
        logger.info("Event %s: option %s paid %d%s", screen, pick["id"], reward,
                    " (JACKPOT)" if jackpot_hit else "")
        if response.get("player_energies"):
            poll["player_energies"] = response["player_energies"]
            energy, _top = energy_now(response["player_energies"])
        else:
            energy -= 1
        for key in ("currency", "player_group", "ranks_best", "ranks_unluckiest"):
            if response.get(key):
                poll[key] = response[key]
        jackpots = ((poll.get("player_group") or {}).get("race")
                    or {}).get("jackpots")
    return done


DRIVERS = {
    "event_horse_race": {
        "label": "Horse race",
        "energy_key": "fodder",
        "read": _horse_read_page,
        "poll": _horse_poll,
        "play": _horse_play,
    },
}


# -- the pass --------------------------------------------------------------

def _record(state, actions):
    """Fold a pass's actions into the running totals and the visible log."""
    totals = state.setdefault("totals", {"actions": 0, "jackpots": 0,
                                         "reward": 0, "expected": 0.0})
    per_option = state.setdefault("by_option", {})
    for action in actions:
        totals["actions"] += 1
        totals["reward"] += action["reward"]
        totals["expected"] = round(totals.get("expected", 0.0)
                                   + action.get("expected", 0), 2)
        if action["jackpot"]:
            totals["jackpots"] += 1
        bucket = per_option.setdefault(str(action["option"]),
                                       {"actions": 0, "jackpots": 0, "reward": 0})
        bucket["actions"] += 1
        bucket["reward"] += action["reward"]
        if action["jackpot"]:
            bucket["jackpots"] += 1
    if actions:
        state["log"] = (actions + state.get("log", []))[:MAX_LOG]


def _archive(screen_now):
    """Mark every event that is no longer running as finished.

    Its file stays: an event that paid out 6,000 trophies is worth keeping a
    record of, and the page shows past events as history.
    """
    for state in list_states():
        if state.get("finished") or state.get("screen") == screen_now:
            continue
        state["finished"] = True
        state["finished_at"] = int(time.time())
        save_state(state)
        logger.info("Event %s has ended - %d action(s), %s earned",
                    state.get("screen"),
                    (state.get("totals") or {}).get("actions", 0),
                    (state.get("totals") or {}).get("reward", 0))


def run(wrapper, village_id, config, overview_html=None):
    """Detect, play and record the running event. Safe to call every cycle.

    Playing is opt-in (events.auto_play) and never starts itself. With it off
    the bot still notices the event and keeps the page's picture current when
    the dashboard asks for it, so the event can be watched before it is handed
    over.
    """
    if not village_id:
        return
    settings = (config or {}).get("events", {}) or {}
    screen, name = detect(overview_html)
    _archive(screen)
    if not screen:
        return

    state = load_state(screen)
    now = int(time.time())
    state.setdefault("screen", screen)
    state.setdefault("first_seen", now)
    state.setdefault("log", [])
    state["name"] = name or state.get("name") or screen
    state["label"] = (DRIVERS.get(screen) or {}).get("label", "")
    state["last_seen"] = now
    state["finished"] = False

    driver = DRIVERS.get(screen)
    if driver is None:
        # An event nobody has written a driver for still gets a row on the page,
        # so it is obvious there is something running that is being missed.
        state["unsupported"] = True
        save_state(state)
        return
    state["unsupported"] = False

    auto = bool(settings.get("auto_play", False))
    asked = bool(state.get("refresh"))
    if not auto and not asked:
        save_state(state)
        return
    state["refresh"] = False

    # The static half (what the options pay, when it ends) is re-read once a
    # day; everything that actually moves comes from the poll.
    #
    # Playing additionally needs a CSRF token, and the poll cannot supply one -
    # it answers with JSON, which carries no "&h=". So an action is only ever
    # taken after a real page has been read this pass, rather than trusting
    # whatever token some earlier screen happened to leave on the wrapper.
    stale_token = auto and not getattr(wrapper, "last_h", None)
    if (not state.get("options") or stale_token
            or now - int(state.get("read_at") or 0) > 86400):
        static = driver["read"](wrapper, village_id, screen)
        if static:
            state.update(static)
            state["read_at"] = now

    poll = driver["poll"](wrapper, village_id, screen)
    if poll is None:
        save_state(state)
        return
    energy, top = energy_now(poll.get("player_energies"),
                             driver.get("energy_key", "fodder"))
    state["snapshot"] = {
        "at": now,
        "energy": round(energy, 2),
        "energy_max": top,
        "energy_rate": energy_rate(poll.get("player_energies"),
                                   driver.get("energy_key", "fodder")),
        "currency": poll.get("currency"),
        "ranks_best": poll.get("ranks_best"),
        "ranks_unluckiest": poll.get("ranks_unluckiest"),
        "group": poll.get("player_group"),
        "options": option_values(
            state.get("options"),
            ((poll.get("player_group") or {}).get("race") or {}).get("jackpots")),
    }
    if auto:
        actions = driver["play"](wrapper, village_id, screen, state, poll,
                                 choice=settings.get("option", "auto"))
        _record(state, actions)
        if actions:
            energy, top = energy_now(poll.get("player_energies"),
                                     driver.get("energy_key", "fodder"))
            state["snapshot"]["energy"] = round(energy, 2)
            state["snapshot"]["currency"] = poll.get("currency", state["snapshot"]["currency"])
            state["snapshot"]["at"] = int(time.time())
    save_state(state)
