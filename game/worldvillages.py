"""The world's own list of every village, from the public map data.

TribalWars publishes map/village.txt: one line per village on the world, as

    id,name,x,y,player_id,points,rank

with the name URL-encoded. It needs no session and no permission - it is the
same file the map tools everyone uses are built on - and one request answers
"who owns this and where is it" for the whole world at once.

The bot's own map cache only covers the ground around its own villages, so
anything further out is a bare id to it: of 53 enemy villages worth naming on
this account, 37 were unknown. Asking the game for each one is 37 requests for
something a single public file already holds, so this is read instead, and kept
for a few hours because ownership changes on the scale of conquests, not
minutes.

Nothing here is authenticated and nothing here writes; a failure returns what
was last cached, or nothing.
"""

import logging
import os
import time
import urllib.parse

import requests

logger = logging.getLogger("WorldVillages")

CACHE_REL = ("cache", "world", "villages_txt.json")
# Conquests are the only thing that moves these, so a few hours is fresh.
TTL = 6 * 3600
# The file is around 600KB on a full world; refuse anything absurd rather than
# read a redirect or an error page into memory as if it were data.
MAX_BYTES = 40 * 1024 * 1024


def parse(text):
    """{village_id: {"name", "x", "y", "owner", "points"}} from the raw file."""
    out = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            out[parts[0]] = {
                "name": urllib.parse.unquote_plus(parts[1]),
                "x": int(parts[2]),
                "y": int(parts[3]),
                # "0" is a barbarian village, the same as everywhere else here.
                "owner": parts[4],
                "points": int(parts[5]),
            }
        except (TypeError, ValueError):
            continue
    return out


def fetch(endpoint):
    """Read map/village.txt for the world behind `endpoint`, or None."""
    if not endpoint or endpoint == "None":
        return None
    url = "%s/map/village.txt" % endpoint.rsplit("/", 1)[0]
    try:
        res = requests.get(url, timeout=(10, 60))
    except requests.RequestException as exc:
        logger.debug("village.txt fetch failed: %s", exc)
        return None
    if res.status_code != 200 or len(res.content) > MAX_BYTES:
        return None
    villages = parse(res.text)
    # A world always has thousands of villages; a handful means we read
    # something that was not the file.
    return villages if len(villages) > 100 else None
