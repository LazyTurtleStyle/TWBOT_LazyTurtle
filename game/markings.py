"""Read the map markings the player set up in game.

TribalWars lets you colour tribes and players on your own map - the panel
behind "Beheer markeringen" - and after a few weeks that colouring is how the
world is read: this lot are the enemy, that lot are the ones we have a pact
with. The dashboard's map was inventing its own three-colour scheme instead,
which meant learning a second language for the same map.

So the markings are read rather than reinvented. The map screen renders them as
a legend: a colour and a label per marking, where the label is the tribe's name
and tag exactly as the game prints it. The label is all there is - the legend
does not carry the tribe's id - so it is matched against the world's public
tribe and player lists, which are flat text files anyone can fetch.

Nothing here changes a marking; it only reads them.
"""

import html as html_module
import logging
import re
import time
from urllib.parse import unquote_plus

from core.filemanager import FileManager

logger = logging.getLogger("Markings")

CACHE = "cache/world/markings.json"
# Markings change when the player edits them, which is rarely and never
# urgently, so this is read about once a day.
REFRESH_SECONDS = 86400

# One legend entry: the colour swatch, then the label the game printed.
_RE_LEGEND = re.compile(
    r'<div class="map_legend" data-id="(\d+)" data-active="(\d)">\s*'
    r'<div style="([^"]*)"></div>\s*<span>(.*?)</span>', re.S)
_RE_RGB = re.compile(r'rgb\((\d+),\s*(\d+),\s*(\d+)\)')
# The "Anderen" row holds tribes and players; the rows above it are our own
# village groups, which are coloured from the group cache instead.
_RE_OTHER_BLOCK = re.compile(
    r'data-category="other"(.*?)</tr>', re.S)


def _hex(style):
    found = _RE_RGB.search(style or "")
    if not found:
        return None
    return "#%02x%02x%02x" % tuple(int(v) for v in found.groups())


def parse_legend(page):
    """The markings as (label, colour) pairs, from the map screen's legend."""
    block = _RE_OTHER_BLOCK.search(page or "")
    if not block:
        return []
    out = []
    for _gid, active, style, label in _RE_LEGEND.findall(block.group(1)):
        colour = _hex(style)
        if not colour or active != "1":
            continue
        out.append({"label": html_module.unescape(re.sub(r"<[^>]+>", "", label)).strip(),
                    "color": colour})
    return out


def _world_list(wrapper, path):
    """One of the world's public data files, as a list of split rows."""
    res = wrapper.get_url(path)
    if res is None or not getattr(res, "text", ""):
        return []
    return [line.split(",") for line in res.text.splitlines() if line.strip()]


def read(wrapper, village_id):
    """Match the markings to tribe and player ids, and cache the result.

    Returns {"tribes": {id: colour}, "players": {id: colour}, "labels": {...}}.
    """
    res = wrapper.get_url(f"game.php?village={village_id}&screen=map")
    if res is None or not getattr(res, "text", ""):
        return None
    legend = parse_legend(res.text)
    if not legend:
        logger.debug("No map markings set up")
        return {"tribes": {}, "players": {}, "when": int(time.time())}

    # tag -> id and name -> id, from the world's own lists. Names are
    # url-encoded in those files, which is why they are unquoted here.
    tribes_by_tag, tribes_by_name = {}, {}
    for row in _world_list(wrapper, "map/ally.txt"):
        if len(row) < 3:
            continue
        tribes_by_tag[unquote_plus(row[2]).lower()] = row[0]
        tribes_by_name[unquote_plus(row[1]).lower()] = row[0]
    players_by_name = {}
    for row in _world_list(wrapper, "map/player.txt"):
        if len(row) < 2:
            continue
        players_by_name[unquote_plus(row[1]).lower()] = row[0]

    tribes, players, labels = {}, {}, {}
    for entry in legend:
        label = entry["label"]
        # The game prints a tribe as "Full Name (TAG)"; anything else is a
        # player, or a tribe whose name has since changed.
        tag = re.search(r"\(([^)]+)\)\s*$", label)
        name = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip().lower()
        tribe_id = (tribes_by_tag.get(tag.group(1).strip().lower()) if tag else None) \
            or tribes_by_name.get(name)
        if tribe_id:
            tribes[tribe_id] = entry["color"]
            labels[tribe_id] = label
            continue
        player_id = players_by_name.get(label.strip().lower())
        if player_id:
            players[player_id] = entry["color"]
            labels["p" + player_id] = label
        else:
            logger.debug("Marking %r matched no tribe or player", label)

    data = {"tribes": tribes, "players": players, "labels": labels,
            "when": int(time.time())}
    FileManager.save_json_file(data, CACHE)
    logger.info("Cached %d tribe and %d player marking(s) from the game",
                len(tribes), len(players))
    return data


def run(wrapper, village_id):
    """Re-read the markings about once a day. Cheap and entirely optional."""
    if not village_id:
        return
    cached = FileManager.load_json_file(CACHE) or {}
    if int(time.time()) - int(cached.get("when") or 0) < REFRESH_SECONDS:
        return
    try:
        read(wrapper, village_id)
    except Exception as exc:  # cosmetic feature; never worth a failed cycle
        logger.debug("Could not read the map markings: %s", exc)
