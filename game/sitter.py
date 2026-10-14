"""
A read-only overview of an account you sit for (vakantievervanging).

The game lets a player look after another player's account while they are
away. You log in as their replacement through the game's portal, and from then
on every page of theirs is the normal page with `t=<their player id>` added.
The bot cannot do that login itself - it only ever holds a world session, and
the replacement login goes through the portal - so the player does it in their
own browser and pastes that session on the dashboard, the same way they paste
their own. It is kept apart from the bot's own session (SESSION_FILE) and only
ever used here.

Nothing here sends anything. A refresh reads three account-wide pages of the
sat account - production, troops, incoming attacks - and stores them for the
dashboard's VV page. The account belongs to somebody else, so it is read on a
slow clock (bot.sitter_refresh_minutes, default 30) and only while the bot is
awake, unless the dashboard asks for a refresh.
"""
import logging
import re
import time
from html import unescape

from core.extractors import Extractor
from core.filemanager import FileManager

SESSION_FILE = "cache/sitter_session.json"
STATE_FILE = "cache/sitter.json"
REQUEST_FILE = "cache/sitter_refresh.json"
DEFAULT_REFRESH_MINUTES = 30

# One row of "Eigen vakantievervangingen" on the settings page: the player's
# profile link, then the details icon that carries the same id.
_RE_SAT_PLAYER = re.compile(
    r'screen=info_player&(?:amp;)?id=(\d+)"[^>]*>([^<]+)</a>\s*'
    r'<span[^>]*data-sat-player="(\d+)"')
OFFENCE = ("axe", "light", "marcher", "ram", "catapult")
DEFENCE = ("spear", "sword", "archer", "heavy")
POP = {"spear": 1, "sword": 1, "axe": 1, "archer": 1, "spy": 2, "light": 4,
       "marcher": 5, "heavy": 6, "ram": 5, "catapult": 8, "knight": 10, "snob": 100}

logger = logging.getLogger("Sitter")


def sat_accounts(html):
    """[{"player_id", "name"}] for every account the settings page says you
    are the replacement for."""
    text = html if isinstance(html, str) else getattr(html, "text", "") or ""
    out, seen = [], set()
    for pid, name, again in _RE_SAT_PLAYER.findall(text):
        if pid == again and pid not in seen:
            seen.add(pid)
            out.append({"player_id": pid, "name": unescape(name).strip()})
    return out


def load_session():
    data = FileManager.load_json_file(SESSION_FILE) or {}
    return data if data.get("cookies") else None


def load_state():
    return FileManager.load_json_file(STATE_FILE) or {}


def refresh_requested():
    """When the dashboard asked for a refresh that has not been served, or None."""
    try:
        data = FileManager.load_json_file(REQUEST_FILE)
    except Exception:
        return None
    return int((data or {}).get("requested") or 0) or None


def _pop(units, kinds):
    return sum(int(units.get(u, 0) or 0) * POP.get(u, 1) for u in kinds)


def summarise(production, troops, incomings, now):
    """The page's picture of the account, from the three parsed pages."""
    by_target = {}
    for cmd in incomings:
        if cmd.get("arrival_ms", 0) / 1000.0 < now:
            continue
        entry = by_target.setdefault(str(cmd.get("target_id")), {"attacks": 0, "nobles": 0, "first": None})
        entry["attacks"] += 1
        label = (cmd.get("game_label") or "").lower()
        if label.startswith(("edel", "noble", "snob")):
            entry["nobles"] += 1
        if entry["first"] is None or cmd["arrival_ms"] < entry["first"]:
            entry["first"] = cmd["arrival_ms"]
    villages = []
    for vid, row in production.items():
        located = troops.get(vid) or {}
        own = located.get("own") or {}
        total = {u: int(own.get(u, 0) or 0)
                 + int((located.get("elsewhere") or {}).get(u, 0) or 0)
                 + int((located.get("moving") or {}).get(u, 0) or 0) for u in own}
        standing = located.get("in_village") or {}
        hits = by_target.get(vid) or {}
        villages.append({
            "id": vid, "name": row.get("name"), "location": row.get("location"),
            "points": row.get("points"), "resources": row.get("resources"),
            "storage_max": row.get("storage_max"), "pop_used": row.get("pop_used"),
            "pop_max": row.get("pop_max"),
            "merchants": [row.get("merchants_free"), row.get("merchants_total")],
            "off_home": _pop(own, OFFENCE), "def_home": _pop(own, DEFENCE),
            "def_standing": _pop(standing, DEFENCE),
            "off_total": _pop(total, OFFENCE), "def_total": _pop(total, DEFENCE),
            "nobles": int(total.get("snob", 0) or 0),
            "units_home": {u: n for u, n in own.items() if n},
            "attacks": hits.get("attacks", 0), "nobles_incoming": hits.get("nobles", 0),
            "first_hit_ms": hits.get("first"),
        })
    villages.sort(key=lambda v: v.get("name") or "")
    return {
        "villages": villages,
        "incomings": sorted(
            ({k: c.get(k) for k in ("command_id", "target_id", "target_coords",
                                    "origin_coords", "origin_name", "player_name",
                                    "game_label", "arrival_ms")}
             for c in incomings if c.get("arrival_ms", 0) / 1000.0 >= now),
            key=lambda c: c["arrival_ms"]),
        "totals": {
            "villages": len(villages),
            "points": sum(int(v.get("points") or 0) for v in villages),
            "off_total": sum(v["off_total"] for v in villages),
            "def_total": sum(v["def_total"] for v in villages),
            "nobles": sum(v["nobles"] for v in villages),
            "attacks": sum(v["attacks"] for v in villages),
            "nobles_incoming": sum(v["nobles_incoming"] for v in villages),
        },
    }


def read_overview(wrapper, player_id, parse_incomings, now=None):
    """Read the sat account's three overview pages. Returns (state, error).

    `parse_incomings(html, server_now)` turns the incomings page into
    commands; it is game/incomings.py's parser, passed in so this module does
    not need a bot-side manager to read one table. Every page is checked to
    really be the sat account's: a session that has run out, or that belongs
    to the player themselves, answers with their own pages, and those must
    never be shown as somebody else's.
    """
    pid = str(player_id)
    base = "game.php?screen=overview_villages&group=0&page=-1&t=%s" % pid
    pages = {}
    for key, query in (("prod", "&mode=prod"), ("units", "&mode=units&type=complete"),
                       ("incomings", "&mode=incomings&subtype=attacks")):
        res = wrapper.get_url(base + query)
        text = getattr(res, "text", "") or ""
        if 'data-bot-protect="forced"' in text:
            return None, "captcha"
        game = Extractor.game_state(res)
        if not game:
            return None, "logged out"
        if str((game.get("player") or {}).get("id")) != pid:
            return None, "not this account's pages (log in as replacement again)"
        pages[key] = res
    game = Extractor.game_state(pages["prod"])
    server_now = int(int(game.get("time_generated") or 0) / 1000) or int(now or time.time())
    production = Extractor.production_overview(pages["prod"])
    troops = Extractor.units_overview_complete(pages["units"])
    incomings = parse_incomings(pages["incomings"].text, server_now)
    state = summarise(production, troops, incomings, now or time.time())
    player = game.get("player") or {}
    state["player"] = {"id": pid, "name": player.get("name"),
                       "points": player.get("points"), "rank": player.get("rank")}
    state["read_at"] = int(now or time.time())
    return state, None
