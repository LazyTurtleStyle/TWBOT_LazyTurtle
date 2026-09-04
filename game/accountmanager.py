"""Set up the in-game (premium) Account Manager from a group -> template plan.

The Account Manager does a village's building, recruiting and research on its
own once a template is applied to it, which is why village.py hands those jobs
over when it is switched on (see Village.account_manager_handles). What it does
not do is stay set up: the building manager works a queue of at most 50 orders
and falls idle when that runs dry, so an account set up on Monday is doing
nothing by Thursday. Re-doing it by hand is one pass per group per screen, from
memory, every few days.

So the plan lives here instead: an ordered list of (group, template) rows per
screen, re-applied once every morning. Rows are applied top to bottom and the
later row wins, which is exactly how the usual layout is built - give [alle] the
barb template, then give [KERK] the church template, and the church villages end
on the church template while every other village keeps the barb one.

Three of the manager's screens are covered, in two shapes:

  building (am_village) and research (am_research) share one mass form: tick
    villages, choose "Gebruik sjabloon" plus a template, confirm.
  troops (am_troops) has no mass-apply. Its form writes one set of target unit
    counts and resource buffers onto every ticked village - which is what the
    in-game "copy from template" dropdown fills in client-side - so applying a
    template there means posting the template's own numbers back ourselves.

Storage (opslag) and delivery (leveringen) are deliberately left alone.

Nothing here guesses an endpoint: every POST reuses the form action the game
just rendered, CSRF token and all, so a page the account cannot open (no
premium, feature off) simply yields no form and the row reports why.
"""

import html as html_module
import json
import logging
import os
import re
import time
from datetime import date

from core.filemanager import FileManager
# The plan file is written by both processes - the dashboard edits it and asks
# for a run, the bot clears the request once it has served it - so it is kept
# under the same cross-process lock the attack queue uses.
from game.attack_scheduler import _Lock

logger = logging.getLogger("AccountManager")

# The plan the user built on the dashboard, and the last state the bot read back
# out of the game. Both are per world (FileManager resolves cache/ to the active
# world's data dir).
PLANS_CACHE = "cache/am_plans.json"
STATE_CACHE = "cache/am_state.json"

# section key -> in-game screen. The key is what the config, the plan file and
# the dashboard all speak; the screen name is only ever used to build a URL.
SCREENS = {
    "building": "am_village",
    "troops": "am_troops",
    "research": "am_research",
}
SECTION_ORDER = ("building", "troops", "research")

# The unit columns of the troop-manager form, in the order the game renders
# them. Also the exact set of field names its save accepts.
TROOP_FIELDS = ("spear", "sword", "axe", "archer", "spy", "light", "marcher",
                "heavy", "ram", "catapult")
BUFFER_FIELDS = ("buffer_wood", "buffer_stone", "buffer_iron", "buffer_pop")

# A village list is paged, `page` being a plain 0-based index (verified live: on
# a 42-village account page 1 renders an empty table). Only a full page is
# followed by another read; the cap is there so a game-side change to the paging
# cannot spin the loop forever.
DEFAULT_PAGE_SIZE = 50
MAX_PAGES = 20

_RE_TEMPLATE_SELECT = re.compile(r'<select name="template".*?</select>', re.S)
_RE_OPTION = re.compile(r'<option value="(\d+)"[^>]*>(.*?)</option>', re.S)
_RE_GROUP = re.compile(
    r'<(a|strong) class="group-menu-item" data-group-id="(\d+)"'
    r' data-group-type="([a-z]+)"[^>]*>(.*?)</\1>', re.S)
_RE_MASS_FORM = re.compile(r'<form action="([^"]*action=village_mass[^"]*)"')
_RE_TROOP_FORM = re.compile(r'<form action="([^"]*action=save_village[^"]*)"')
_RE_ROW = re.compile(r'<tr class="row_[ab]">(.*?)</tr>', re.S)
_RE_CELL = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
# The two checkboxes carry their attributes in a different order, so neither
# pattern may assume value follows name directly.
_RE_MASS_CHECKBOX = re.compile(r'name="villages\[\]"[^>]*value="(\d+)"')
_RE_TROOP_CHECKBOX = re.compile(r'name="edit\[\]"[^>]*value="(\d+)"')
_RE_VILLAGE_NAME = re.compile(r'screen=info_village[^"]*"[^>]*>(.*?)</a>', re.S)
_RE_DATA_FIELD = re.compile(r'data-field="(\w+)">([\d.,]*)<')
_RE_TROOP_TEMPLATES = re.compile(
    r'Accountmanager\.initTroopManagement\((\[.*?\])\);', re.S)
# The screen's own "villages per page" box: a page holding fewer villages than
# this is the last one, which saves reading a second page just to find it empty.
_RE_PAGE_SIZE = re.compile(r'name="page_size"[^>]*value="(\d+)"')
# How the game complains about a form it did not accept.
_RE_ERROR_BOX = re.compile(r'class="error_box"[^>]*>(.*?)</div>', re.S)


def _text(raw):
    """Tag-stripped, entity-decoded, whitespace-collapsed cell text."""
    return re.sub(r"\s+", " ", html_module.unescape(
        re.sub(r"<[^>]+>", " ", raw or ""))).strip()


def _form_url(raw):
    """The game's own form action, as a URL the wrapper can be handed."""
    return html_module.unescape(raw or "").lstrip("/")


def _plans_path(path=None):
    """The plan file, honouring a path the caller resolved itself.

    The dashboard has to pass one: its process has no per-world data root, so
    FileManager there would answer with the default world's file whichever world
    is being looked at.
    """
    return path or FileManager.get_path(PLANS_CACHE)


def _normalize(raw):
    """Always the same shape, whatever is (or is not) in the file."""
    raw = raw if isinstance(raw, dict) else {}
    plans = {}
    for section in SECTION_ORDER:
        rows = raw.get(section)
        plans[section] = [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []
    plans["run_now"] = bool(raw.get("run_now"))
    plans["refresh"] = bool(raw.get("refresh"))
    return plans


def _read_plans(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def load_plans(path=None):
    """The saved group -> template plan: {section: [{group_id, template_id,
    group_name, template_name}]} plus the dashboard's request flags.

    Reads are lock-free, because writes are atomic."""
    return _normalize(_read_plans(_plans_path(path)))


def update_plans(mutator, path=None):
    """Read -> mutate -> write the plan under the cross-process lock.

    `mutator(plans)` edits the dict in place. Every write goes through here so
    the dashboard asking for a run cannot be lost by the bot writing back a
    cleared flag it read a moment earlier (or the other way round).
    """
    target = _plans_path(path)
    with _Lock(target):
        plans = _normalize(_read_plans(target))
        mutator(plans)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = "%s.tmp.%d" % (target, os.getpid())
        with open(tmp, "w") as handle:
            json.dump(plans, handle, indent=2)
        os.replace(tmp, target)  # atomic, so a reader never sees half a file
    return plans


def save_plans(plans, path=None):
    """Overwrite the whole plan (the dashboard replaces one section at a time
    through update_plans instead)."""
    return update_plans(lambda current: current.update(plans), path=path)


def load_state():
    return FileManager.load_json_file(STATE_CACHE) or {}


def save_state(state):
    FileManager.save_json_file(state, STATE_CACHE)


# -- reading ---------------------------------------------------------------

def parse_groups(page):
    """Every village group the manager offers, in menu order.

    The currently selected group renders as <strong>&gt;name&lt;</strong>
    instead of a link, so both tags are read and the brackets the menu wraps
    names in are stripped. Groups are also listed twice on some screens, hence
    the de-duplication.
    """
    groups, seen = [], set()
    for _tag, gid, gtype, label in _RE_GROUP.findall(page):
        if gid in seen:
            continue
        seen.add(gid)
        name = _text(label).strip("[]<> ")
        groups.append({"id": gid, "name": name or gid, "type": gtype})
    return groups


def parse_mass_templates(page):
    """The template dropdown of the building / research mass form."""
    block = _RE_TEMPLATE_SELECT.search(page)
    if not block:
        return []
    return [{"id": tid, "name": _text(label)}
            for tid, label in _RE_OPTION.findall(block.group(0))]


def parse_troop_templates(page):
    """The troop templates, read from the bootstrap call that seeds the page.

    Each carries its own unit targets and resource buffers, which is all the
    save needs - the in-game dropdown copies exactly these numbers into the
    form before the player hits Opslaan.
    """
    match = _RE_TROOP_TEMPLATES.search(page)
    if not match:
        return []
    try:
        raw = json.loads(html_module.unescape(match.group(1)))
    except ValueError:
        logger.warning("Could not read the troop template list")
        return []
    templates = []
    for entry in raw if isinstance(raw, list) else []:
        templates.append({
            "id": str(entry.get("id") or ""),
            "name": entry.get("template") or "",
            "units": {f: str(entry.get(f) or "0") for f in TROOP_FIELDS},
            "buffers": {f: str(entry.get(f) or "0") for f in BUFFER_FIELDS},
        })
    return [t for t in templates if t["id"]]


def parse_mass_villages(page, section):
    """The village rows of the building / research screen.

    Columns differ by one: building also reports how many build orders are left
    of the manager's queue of 50, which is the number that decides whether it is
    still doing anything.
    """
    villages = []
    for row in _RE_ROW.findall(page):
        vid = _RE_MASS_CHECKBOX.search(row)
        if not vid:
            continue
        cells = [_text(c) for c in _RE_CELL.findall(row)]
        name = _RE_VILLAGE_NAME.search(row)
        entry = {
            "id": vid.group(1),
            "name": _text(name.group(1)) if name else vid.group(1),
            "template": cells[1] if len(cells) > 1 else "",
            "status": cells[-2] if len(cells) > 2 else "",
            "orders": cells[2] if section == "building" and len(cells) > 3 else "",
        }
        villages.append(entry)
    return villages


def parse_troop_villages(page):
    """The village rows of the troop manager, with the targets each one holds."""
    villages = []
    for row in _RE_ROW.findall(page):
        vid = _RE_TROOP_CHECKBOX.search(row)
        if not vid:
            continue
        name = _RE_VILLAGE_NAME.search(row)
        fields = dict(_RE_DATA_FIELD.findall(row))
        villages.append({
            "id": vid.group(1),
            "name": _text(name.group(1)) if name else vid.group(1),
            "units": {f: fields.get(f, "") for f in TROOP_FIELDS},
            "buffers": {f: fields.get(f, "") for f in BUFFER_FIELDS},
        })
    return villages


def read_page(wrapper, village_id, section, group_id="0", page=0):
    """One page of a manager screen, parsed into templates, groups, villages
    and the form the page wants its answer posted to.

    Returns None when the page could not be read at all (dead session, captcha,
    or a world where the manager is not available), so callers can tell "no
    villages in this group" apart from "no page".
    """
    screen = SCREENS[section]
    res = wrapper.get_url(
        f"game.php?village={village_id}&screen={screen}"
        f"&group={group_id}&page={page}")
    if res is None or not getattr(res, "text", ""):
        return None
    body = res.text
    if "am_village_edit" not in body and "am_troops_edit" not in body \
            and "group-menu-item" not in body:
        # Not the manager screen: an account without premium is redirected to
        # the overview, which has none of these markers.
        return None
    size = _RE_PAGE_SIZE.search(body)
    if section == "troops":
        form = _RE_TROOP_FORM.search(body)
        parsed = {
            "templates": parse_troop_templates(body),
            "groups": parse_groups(body),
            "villages": parse_troop_villages(body),
            "form": _form_url(form.group(1)) if form else None,
        }
    else:
        form = _RE_MASS_FORM.search(body)
        parsed = {
            "templates": parse_mass_templates(body),
            "groups": parse_groups(body),
            "villages": parse_mass_villages(body, section),
            "form": _form_url(form.group(1)) if form else None,
        }
    parsed["page_size"] = int(size.group(1)) if size else DEFAULT_PAGE_SIZE
    return parsed


def read_section(wrapper, village_id, section, group_id="0"):
    """A whole group's worth of a manager screen, following its paging.

    An account whose villages fit on one page costs exactly one request: the
    screen states its own page size, so a short page is known to be the last one
    without asking for the next.
    """
    first = read_page(wrapper, village_id, section, group_id, page=0)
    if first is None:
        return None
    seen = {v["id"] for v in first["villages"]}
    pages = [first]
    page_size = first.get("page_size") or DEFAULT_PAGE_SIZE
    while len(seen) >= page_size * len(pages) and len(pages) < MAX_PAGES:
        extra = read_page(wrapper, village_id, section, group_id,
                          page=len(pages))
        if extra is None:
            break
        fresh = [v for v in extra["villages"] if v["id"] not in seen]
        if not fresh:
            break
        seen.update(v["id"] for v in fresh)
        extra["villages"] = fresh
        pages.append(extra)
    combined = dict(first)
    combined["villages"] = [v for page in pages for v in page["villages"]]
    combined["pages"] = pages
    return combined


# -- applying --------------------------------------------------------------

def _post_error(res):
    """The game's own complaint about the form just posted, or None.

    Worth reading rather than assuming a 200 means it worked: a template
    deleted between the read and the post, or a village that left the group,
    comes back as a rendered page carrying an error box.
    """
    if res is None:
        return "no response"
    found = _RE_ERROR_BOX.search(getattr(res, "text", "") or "")
    if not found:
        return None
    return _text(found.group(1)) or "the game rejected the form"


def _apply_mass_page(wrapper, page, template_id):
    """Post one page of the building / research mass form."""
    ids = [v["id"] for v in page["villages"]]
    if not ids or not page.get("form"):
        return 0, None
    res = wrapper.post_url(page["form"], data={
        "action": "apply",
        "template": str(template_id),
        "villages[]": ids,
    })
    error = _post_error(res)
    return (0, error) if error else (len(ids), None)


def _apply_troop_page(wrapper, page, template):
    """Post one page of the troop manager: the template's own numbers, onto
    every village of the page. This is what the in-game copy-from-template
    dropdown does before the player saves."""
    ids = [v["id"] for v in page["villages"]]
    if not ids or not page.get("form"):
        return 0, None
    data = {"template_id": str(template["id"]), "save": "Opslaan",
            "edit[]": ids}
    data.update(template["units"])
    data.update(template["buffers"])
    res = wrapper.post_url(page["form"], data=data)
    error = _post_error(res)
    return (0, error) if error else (len(ids), None)


def apply_row(wrapper, village_id, section, group_id, template_id):
    """Apply one (group, template) row and report what happened.

    Returns {ok, villages, group_id, template_id, template_name, group_name,
    error}. Errors are values, never exceptions: one unreadable group must not
    stop the rest of the morning's plan.
    """
    result = {"section": section, "group_id": str(group_id),
              "template_id": str(template_id), "group_name": "",
              "template_name": "", "villages": 0, "ok": False, "error": None}
    state = read_section(wrapper, village_id, section, group_id)
    if state is None:
        result["error"] = "screen unavailable"
        return result
    for group in state["groups"]:
        if group["id"] == str(group_id):
            result["group_name"] = group["name"]
    template = None
    for entry in state["templates"]:
        if entry["id"] == str(template_id):
            template = entry
            result["template_name"] = entry["name"]
    if template is None:
        result["error"] = "template %s no longer exists" % template_id
        return result
    if not state["villages"]:
        # An empty group is a plan that has gone stale, not a failure: say so
        # and carry on.
        result["ok"] = True
        result["error"] = "no villages in this group"
        return result
    if not state.get("form"):
        result["error"] = "no form on the page"
        return result
    done, failed = 0, None
    for page in state["pages"]:
        if section == "troops":
            count, error = _apply_troop_page(wrapper, page, template)
        else:
            count, error = _apply_mass_page(wrapper, page, template_id)
        done += count
        failed = failed or error
    result["villages"] = done
    result["ok"] = done > 0 and not failed
    result["error"] = failed
    return result


def refresh(wrapper, village_id, sections=SECTION_ORDER):
    """Re-read the manager's templates, groups and per-village state.

    This is what fills the dashboard's dropdowns, so it runs on demand as well
    as after every applied plan - one request per screen at group [alle].
    """
    state = load_state()
    snapshots = state.get("sections") or {}
    for section in sections:
        read = read_section(wrapper, village_id, section, group_id="0")
        if read is None:
            logger.warning("Could not read the %s screen of the Account "
                           "Manager", section)
            continue
        snapshots[section] = {
            "when": int(time.time()),
            "templates": read["templates"],
            "groups": read["groups"],
            "villages": read["villages"],
        }
    state["sections"] = snapshots
    state["when"] = int(time.time())
    save_state(state)
    return state


def apply_plans(wrapper, village_id, plans=None, source="bot"):
    """Walk the whole plan, in order, and record the outcome.

    Order is the point: rows are applied top to bottom so a later row can
    deliberately overwrite an earlier one for the villages the two groups share.
    """
    plans = plans or load_plans()
    rows = []
    for section in SECTION_ORDER:
        for row in plans.get(section) or []:
            group_id = str(row.get("group_id") or "")
            template_id = str(row.get("template_id") or "")
            if not group_id or not template_id:
                continue
            outcome = apply_row(wrapper, village_id, section, group_id,
                                template_id)
            rows.append(outcome)
            logger.info(
                "Account Manager %s: group %s -> %s (%d village(s))%s",
                section, outcome["group_name"] or group_id,
                outcome["template_name"] or template_id, outcome["villages"],
                "" if outcome["ok"] else " FAILED: %s" % outcome["error"])
    state = load_state()
    state["last_result"] = {"when": int(time.time()), "source": source,
                            "rows": rows}
    save_state(state)
    if rows:
        # The state the plan just produced is the state worth showing, so read
        # it back while the pages are certain to be fresh.
        refresh(wrapper, village_id,
                sections=[s for s in SECTION_ORDER if plans.get(s)])
    return rows


def run(wrapper, village_id, config, active_hours=True):
    """Once a day, re-apply the plan; also serve the dashboard's requests.

    The daily pass is tied to active hours, so "once a day" lands on the first
    cycle of the morning rather than at a fixed robotic minute, and it needs
    account_manager.auto_setup on top of a plan - a plan can be built and
    applied by hand for a while first. What the dashboard asks for explicitly
    (apply now, refresh) is served whenever it is asked, night or not.
    """
    if not village_id:
        return
    plans = load_plans()
    has_rows = any(plans.get(section) for section in SECTION_ORDER)
    settings = (config or {}).get("account_manager", {}) or {}

    # Requests are cleared before they are served, not after: a request that
    # somehow crashes the pass has to be a one-off, not something the bot
    # retries every cycle for the rest of the day.
    if plans.get("refresh"):
        update_plans(lambda p: p.update({"refresh": False}))
        refresh(wrapper, village_id)

    if plans.get("run_now") and has_rows:
        update_plans(lambda p: p.update({"run_now": False}))
        logger.info("Applying the Account Manager plan (asked from the "
                    "dashboard)")
        apply_plans(wrapper, village_id, plans, source="dashboard")
        return

    if not active_hours or not settings.get("auto_setup", False) or not has_rows:
        return
    today = date.today().isoformat()
    state = load_state()
    if state.get("last_run") == today:
        return
    logger.info("Applying the Account Manager plan for %s", today)
    apply_plans(wrapper, village_id, plans, source="bot")
    # Written after the pass, so a crash midway retries next cycle rather than
    # skipping the day.
    state = load_state()
    state["last_run"] = today
    state["last_run_ts"] = int(time.time())
    save_state(state)
