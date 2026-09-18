"""Read and write the personal note the game keeps on any village.

TribalWars lets you pin a private note to any village on the map - your own or
somebody else's - shown on its info page. It is the natural place to keep what
you have worked out about an enemy village, because it is attached to the
village rather than to a spreadsheet you have to go and find.

What it is for here: after a defence holds, the attacker's nuke is gone and that
village cannot hit again until it is rebuilt, which takes weeks. Knowing which
of his villages are in that state is the difference between reading the next
wave and guessing at it - and the reports already say so, one report at a time,
weeks before you need the answer.

The game saves a note with

    TribalWars.post("api", {ajaxaction: "village_note_edit"},
                    {village_id: <id>, note: <text>})

which replaces whatever was there. Replacing is the dangerous part: a note you
wrote by hand is exactly the sort of thing that is not written down anywhere
else, so nothing here writes blind. `read_note` fetches the current text first,
`compose` folds the new line into it instead of over it, and a line that is
already present is left alone rather than added twice.

Like game/livetroops.py this runs from the web process with the bot's stored
session, so it never raises and reports its reasons as strings.
"""

import html as html_module
import logging
import re

import requests

from core.extractors import Extractor

logger = logging.getLogger("VillageNotes")

# The info page renders the player's own note inside this block; the body is
# empty (and the whole widget hidden) when there is no note. The block is found
# by its id and then the body inside it - matching the block's own closing tags
# is what a first attempt did, and the nested divs mean the terminator swallows
# the body's own </div>, so the body is never found.
_OWN_NOTE_MARKER = '<div id="own_village_note">'
_RE_NOTE_BODY = re.compile(r'<div class="village-note-body">(.*?)</div>', re.S)
_RE_CSRF = re.compile(r"&h=(\w+)")
# Who and where the village is, off the same page the note comes from. Most
# enemy villages are not in the bot's map cache - it only covers the ground
# around its own - so without this a dead-clear list is a column of bare ids.
_RE_PAGE_NAME = re.compile(r"<h2>(.*?)</h2>", re.S)
_RE_PAGE_COORDS = re.compile(r"screen=map&(?:amp;)?x=(\d+)&(?:amp;)?y=(\d+)")
_RE_PAGE_OWNER_ID = re.compile(r"VillageInfo\.player_id\s*=\s*(\d+)")
_RE_PAGE_OWNER = re.compile(
    r'screen=info_player&(?:amp;)?id=\d+"[^>]*>(.*?)</a>', re.S)


def _page_village(page_text):
    """Name, coordinates and owner of the village whose info page this is."""
    about = {}
    name = _RE_PAGE_NAME.search(page_text or "")
    if name:
        about["name"] = html_module.unescape(
            re.sub(r"<[^>]+>", "", name.group(1))).strip()
    where = _RE_PAGE_COORDS.search(page_text or "")
    if where:
        about["coords"] = [int(where.group(1)), int(where.group(2))]
    owner_id = _RE_PAGE_OWNER_ID.search(page_text or "")
    if owner_id:
        about["owner"] = owner_id.group(1)
    owner = _RE_PAGE_OWNER.search(page_text or "")
    if owner:
        about["owner_name"] = html_module.unescape(
            re.sub(r"<[^>]+>", "", owner.group(1))).strip()
    return about


def _session(cookies, endpoint, user_agent=None):
    if not cookies or not endpoint or endpoint == "None":
        return None, None, None
    session = requests.Session()
    for key, value in cookies.items():
        session.cookies.set(key, value)
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    return session, endpoint.rsplit("/", 1)[0], headers


def _note_text(page_text):
    """The note currently on the village, as plain text ("" when there is none).

    Returns None when the block cannot be found at all, which the callers treat
    as "do not write" - an unreadable note is not the same as an empty one, and
    the difference is somebody's notes.
    """
    at = (page_text or "").find(_OWN_NOTE_MARKER)
    if at < 0:
        return None
    # Bounded so a later note - the page also lists notes other players have
    # shared - can never be read as this one.
    body = _RE_NOTE_BODY.search(page_text, at, at + 4000)
    if not body:
        return None
    text = re.sub(r"<br\s*/?>", "\n", body.group(1))
    text = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(text).strip()


def read_note(village_id, cookies, endpoint, user_agent=None, home_village=None):
    """The note on `village_id` right now, plus the token needed to change it.

    Returns {"ok": True, "note": str, "csrf": str} or {"ok": False, "reason": ...}.
    """
    session, base, headers = _session(cookies, endpoint, user_agent)
    if session is None:
        return {"ok": False, "reason": "no_session"}
    url = "%s/game.php?village=%s&screen=info_village&id=%s" % (
        base, home_village or village_id, village_id)
    try:
        res = session.get(url, headers=headers, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.debug("note read failed: %s", exc)
        return {"ok": False, "reason": "request_failed", "error": str(exc)}
    text = getattr(res, "text", "") or ""
    if 'data-bot-protect="forced"' in text:
        return {"ok": False, "reason": "logged_out", "error": "bot protection"}
    if not Extractor.game_state(res):
        return {"ok": False, "reason": "logged_out"}
    note = _note_text(text)
    if note is None:
        return {"ok": False, "reason": "no_note_widget"}
    token = _RE_CSRF.search(text)
    result = {"ok": True, "note": note,
              "csrf": token.group(1) if token else None}
    result.update(_page_village(text))
    return result


def compose(existing, line, replace=None):
    """The note to save: `line` added to `existing` without losing it.

    `replace`, when given, is a test for lines this bot wrote itself earlier;
    those are taken out so a verdict that changed (dead, then alive again)
    reads as one line, not a history. Nothing it does not recognise is touched.

    Returns None when the note would not change, so a second run over the same
    reports is a no-op rather than a note that repeats itself. Newest first,
    because the note is read at a glance in a tooltip.
    """
    existing = (existing or "").strip()
    line = (line or "").strip()
    if not line:
        return None
    lines = existing.splitlines() if existing else []
    kept = [l for l in lines
            if l.strip() == line or not (replace and replace(l))]
    if len(kept) == len(lines) and line in existing:
        return None
    kept = [l for l in kept if l.strip() != line]
    rest = "\n".join(kept).strip()
    return "%s\n%s" % (line, rest) if rest else line


def write_note(village_id, note, cookies, endpoint, user_agent, csrf,
               home_village=None):
    """Save `note` on `village_id`, replacing what is there.

    Callers are expected to have built `note` with compose() from a read_note()
    result; this is the raw save the game offers and it does not merge anything
    itself.
    """
    session, base, headers = _session(cookies, endpoint, user_agent)
    if session is None:
        return {"ok": False, "reason": "no_session"}
    if not csrf:
        return {"ok": False, "reason": "no_token"}
    headers = dict(headers)
    headers.update({"TribalWars-Ajax": "1", "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01"})
    url = "%s/game.php?village=%s&screen=api&ajaxaction=village_note_edit&h=%s" % (
        base, home_village or village_id, csrf)
    try:
        res = session.post(url, data={"village_id": str(village_id),
                                      "note": note, "h": csrf},
                           headers=headers, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.debug("note write failed: %s", exc)
        return {"ok": False, "reason": "request_failed", "error": str(exc)}
    try:
        payload = res.json()
    except ValueError:
        return {"ok": False, "reason": "not_json", "status": res.status_code}
    if isinstance(payload, dict) and payload.get("error"):
        return {"ok": False, "reason": "refused", "error": payload["error"]}
    saved = (payload or {}).get("response") or payload or {}
    return {"ok": True, "note": saved.get("note", note)}
