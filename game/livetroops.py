"""
Read a village's troops at home *right now*, from the web process.

The dashboard's forms are prefilled from cached readings: a village's own
``available_troops`` snapshot (rewritten only when the bot next runs that
village, so it can be an hour old or, on a village the bot has not run yet,
simply empty) and the account-wide troop-location reading the bot refreshes
every few minutes. Both are good enough to plan with and neither is what you
want to stake a snipe on - by the time you are arming one, "what is standing
there this second" is the question, and the answer decides whether the send is
worth making at all.

This is that answer. It replays one authenticated GET of the rally point with
the bot's stored session cookies - the same page, and the same reading, the
send itself will use when it opens the rally point to build the command - and
parses the "(N)" select-all links next to each unit input, which the game
renders only for units actually standing in the village.

It never raises and it never writes anything: a dead session, bot protection or
a redirect comes back as a reason string for the caller to show.
"""

import logging

import requests

from core.extractors import Extractor

logger = logging.getLogger("LiveTroops")

# Every unit the rally point offers an input for. A unit with none at home has
# no "(N)" link, so it is absent from the parse rather than zero - the caller
# gets an explicit 0 for it instead of a hole.
PLACE_UNITS = ["spear", "sword", "axe", "archer", "spy", "light", "marcher",
               "heavy", "ram", "catapult", "knight", "snob"]


def read_home_troops(village_id, cookies, endpoint, user_agent=None):
    """Units standing in `village_id` this second, off the live rally point.

    Returns {"ok": True, "units": {unit: int}} or {"ok": False, "reason": ...}.
    Reasons: no_session, request_failed, logged_out (the session cookie is
    dead or bot protection is in the way), no_form (a 200 that was not the
    send form).
    """
    if not cookies or not endpoint or endpoint == "None":
        return {"ok": False, "reason": "no_session"}

    base = endpoint.rsplit("/", 1)[0]
    url = "%s/game.php?village=%s&screen=place" % (base, village_id)

    session = requests.Session()
    for key, value in cookies.items():
        session.cookies.set(key, value)
    headers = {"Accept": "text/html,application/xhtml+xml"}
    if user_agent:
        headers["User-Agent"] = user_agent
    try:
        res = session.get(url, headers=headers, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.debug("live troop read failed: %s", exc)
        return {"ok": False, "reason": "request_failed", "error": str(exc)}

    text = getattr(res, "text", "") or ""
    if 'data-bot-protect="forced"' in text:
        return {"ok": False, "reason": "logged_out", "error": "bot protection"}
    # Every logged-in game page carries the updateGameData blob; the login
    # redirect we get on a dead cookie does not.
    if not Extractor.game_state(res):
        return {"ok": False, "reason": "logged_out"}

    home = Extractor.units_in_place(res)
    if not home:
        # An empty reading is ambiguous on its own - a village with nothing at
        # home renders no "(N)" links at all, and so does a page that was never
        # the send form. The unit inputs are always rendered, so they tell the
        # two apart, and they are worth telling apart: one means the village is
        # empty, the other means the session needs looking at.
        form = {name for name, _ in Extractor.attack_form(res)}
        if not form.intersection(set(PLACE_UNITS)):
            return {"ok": False, "reason": "no_form"}

    return {"ok": True, "units": {u: int(home.get(u, 0) or 0) for u in PLACE_UNITS}}
