"""Feed the coin village, so minting never stops for want of resources.

Coins are how noble limits are bought, and minting is a logistics problem
rather than a clicking one. The economical way to do it is to pick one village,
put the coin-cost flag in it (and whatever discounts are going), then pour the
whole account's production into that one warehouse. TribalWars now mints
automatically for eight hours at a time, so the only recurring job left is the
pouring - which is a market screen, a group filter, and a number typed into
every village's row.

That is this module. Every so often it opens the coin village's "grondstoffen
opvragen" screen filtered to a group, works out what each village can spare and
what the target can still hold, and asks for it.

Two things decide the numbers, and both are read rather than assumed:

  what a village can send - its own stock minus whatever should stay behind,
    capped by its merchants, which the screen states as data-capacity;
  what the target can take - its warehouse minus what is in it and what is
    already on the way, because resources that arrive at a full warehouse are
    simply lost.

Requests are made in the ratio a coin actually costs (read off the academy, so
the flag's discount is already in it), because a warehouse full of iron mints
nothing. "even" is offered for the case where the point is just to empty the
account into one place.

Nothing here mints, activates auto-minting, or moves a flag: those are one-off
decisions with real cost, and the module's job is the part that repeats.
"""

import html as html_module
import json
import logging
import re
import time

from core.filemanager import FileManager

logger = logging.getLogger("Minter")

STATE = "cache/minting.json"

# What a coin costs before any discount. Only used to express the ratio when the
# academy cannot be read; the live cost is preferred, since a flag changes it.
BASE_COIN_COST = {"wood": 28000, "stone": 30000, "iron": 25000}
RESOURCES = ("wood", "stone", "iron")

# One merchant carries this much. The screen gives a village's total capacity
# directly, so this is only a fallback.
MERCHANT_CARRY = 1000

_RE_ROW = re.compile(
    r'<tr data-capacity="(\d+)"[^>]*class="supply_location"[^>]*data-village="(\d+)"'
    r'(.*?)</tr>', re.S)
_RE_RES = re.compile(r'data-res-type="(wood|stone|iron)" data-res="(\d+)"')
_RE_NAME = re.compile(r'screen=info_village[^"]*"[^>]*>(.*?)</a>', re.S)
_RE_DURATION = re.compile(r'<td>(\d+:\d{2}:\d{2})</td>')
_RE_TRADERS = re.compile(r'class="traders">(\d+)/(\d+)<')
_RE_STORAGE = re.compile(r'<td>(\d+)</td>\s*<td class="traders"')
_RE_CALL_FORM = re.compile(r'<form[^>]*action="([^"]*action=call[^"]*)"')

# The academy prints the live coin cost in three ids, discount already applied.
_RE_COINS = re.compile(r'(?s)Goudmunten.*?Totaal:.*?<td[^>]*>(.*?)</td>')
# Auto-minting is idle exactly when the page still offers to start a session;
# while one is running there is nothing to start, only something to cancel.
_RE_AUTO_START = re.compile(r'action=start_auto_minting_session')
_RE_AUTO_STATUS = re.compile(r'class="[^"]*auto-minting-status"[^>]*title="([^"]*)"')


def _num(raw):
    digits = re.sub(r"[^\d]", "", html_module.unescape(
        re.sub(r"<[^>]+>", "", raw or "")))
    return int(digits) if digits else 0


def _text(raw):
    return re.sub(r"\s+", " ", html_module.unescape(
        re.sub(r"<[^>]+>", " ", raw or ""))).strip()


def _coin_costs(page):
    """The three coin costs the academy prints, discount included.

    Read by slicing rather than by one pattern: the game writes a thousands
    separator as its own element ("21<span>.</span>560"), so anything
    non-greedy stops at 21 and quietly reports a coin costing twenty-one wood.
    """
    costs = {}
    for kind in RESOURCES:
        marker = 'id="coin_cost_%s"' % kind
        at = page.find(marker)
        if at < 0:
            continue
        chunk = page[at:at + 220]
        for other in RESOURCES:                     # stop at the next resource
            cut = chunk.find('id="coin_cost_%s"' % other, len(marker))
            if cut > 0:
                chunk = chunk[:cut]
        costs[kind] = _num(chunk)
    return costs


def _totals(page):
    """Resources already walking to this village, from its three total cells.

    Read the same way as the coin costs and for the same reason: the game
    writes a thousands separator as its own element, so anything that stops at
    the first closing tag reports 468.090 as 468 - or, matched loosely across
    the table, picks up unrelated numbers entirely. Under-reading this inflates
    the headroom, which is the one number that must never be too generous:
    resources arriving at a full warehouse are destroyed.
    """
    totals = {}
    for kind in RESOURCES:
        marker = 'id="total_%s"' % kind
        at = page.find(marker)
        if at < 0:
            continue
        chunk = page[at:at + 220]
        cut = chunk.find("</td>")
        totals[kind] = _num(chunk[:cut] if cut > 0 else chunk)
    return {r: totals.get(r, 0) for r in RESOURCES}


def load_state():
    return FileManager.load_json_file(STATE) or {}


def save_state(state):
    FileManager.save_json_file(state, STATE)


# -- reading ---------------------------------------------------------------

def read_academy(wrapper, village_id):
    """Coins held, what one costs now, and whether the game is minting itself.

    The cost is read rather than computed because the coin flag and any active
    discount are already baked into what the screen prints - which also makes it
    the honest source for the ratio to request in.
    """
    res = wrapper.get_url(f"game.php?village={village_id}&screen=snob")
    if res is None or not getattr(res, "text", ""):
        return None
    page = res.text
    out = {"coins": None, "cost": None, "auto_mint": None, "discount": None,
           "auto_status": None}
    cost = _coin_costs(page)
    if all(cost.get(r) for r in RESOURCES):
        out["cost"] = {r: cost[r] for r in RESOURCES}
        # How much cheaper than an undiscounted coin, for the overview: the
        # flag and any active discount are already in what the screen printed.
        out["discount"] = round(1 - cost["wood"] / float(BASE_COIN_COST["wood"]), 3)
    coins = _RE_COINS.search(page)
    if coins:
        out["coins"] = _num(coins.group(1))
    out["auto_mint"] = not bool(_RE_AUTO_START.search(page))
    status = _RE_AUTO_STATUS.search(page)
    out["auto_status"] = html_module.unescape(status.group(1)) if status else None
    return out


def read_call(wrapper, village_id, group="0"):
    """The request screen: who can send what, and what is already on the way."""
    res = wrapper.get_url(
        f"game.php?village={village_id}&screen=market&mode=call&group={group}")
    if res is None or not getattr(res, "text", ""):
        return None
    page = res.text
    if "supply_location" not in page and "opvragen" not in page.lower():
        return None
    form = _RE_CALL_FORM.search(page)
    villages = []
    for capacity, vid, body in _RE_ROW.findall(page):
        stock = {kind: int(value) for kind, value in _RE_RES.findall(body)}
        name = _RE_NAME.search(body)
        duration = _RE_DURATION.search(body)
        traders = _RE_TRADERS.search(body)
        storage = _RE_STORAGE.search(body)
        villages.append({
            "id": vid,
            "name": _text(name.group(1)) if name else vid,
            "duration": duration.group(1) if duration else "",
            "capacity": int(capacity),
            "traders": int(traders.group(1)) if traders else 0,
            "traders_max": int(traders.group(2)) if traders else 0,
            "storage": int(storage.group(1)) if storage else 0,
            "stock": {r: stock.get(r, 0) for r in RESOURCES},
        })
    incoming = _totals(page)
    return {"villages": villages, "incoming": incoming,
            "form": html_module.unescape(form.group(1)).lstrip("/") if form else None}


# -- deciding --------------------------------------------------------------

def plan_request(villages, headroom, cost, ratio="coin", keep=0, minimum=1000):
    """How much to ask each village for.

    Walked in order, each village giving what it can spare until the target's
    warehouse is accounted for - so the nearest villages (which the screen lists
    by travel time) fill it first and the rest are left alone rather than
    sending resources that would arrive to a full warehouse.
    """
    if ratio == "coin" and cost:
        total = float(sum(cost.values()))
        share = {r: cost[r] / total for r in RESOURCES}
    else:
        share = {r: 1.0 / len(RESOURCES) for r in RESOURCES}

    budget = {r: max(0, int(headroom * share[r])) for r in RESOURCES}
    asks = []
    for village in villages:
        if not any(budget.values()):
            break
        spare = {r: max(0, village["stock"].get(r, 0) - keep) for r in RESOURCES}
        want = {r: min(spare[r], budget[r]) for r in RESOURCES}
        # Merchants are the hard limit on what one village can move at once.
        capacity = village.get("capacity") or village.get("traders", 0) * MERCHANT_CARRY
        if sum(want.values()) > capacity:
            # Trim proportionally rather than dropping a resource entirely: a
            # part load in the right ratio is still a coin.
            scale = capacity / float(sum(want.values()))
            want = {r: int(want[r] * scale) for r in RESOURCES}
        if sum(want.values()) < minimum:
            continue
        for r in RESOURCES:
            budget[r] -= want[r]
        asks.append({"id": village["id"], "name": village["name"],
                     "duration": village["duration"], "amounts": want})
    return asks


def send_request(wrapper, form, asks):
    """Post the filled-in request form. Returns how many villages were asked."""
    if not form or not asks:
        return 0
    data = {"target_id": "0", "select-village": []}
    for ask in asks:
        data["select-village"].append(ask["id"])
        for kind in RESOURCES:
            amount = ask["amounts"].get(kind, 0)
            data["resource[%s][%s]" % (ask["id"], kind)] = str(amount) if amount else ""
    res = wrapper.post_url(form, data=data)
    return len(asks) if res is not None else 0


# -- the pass --------------------------------------------------------------

def run(wrapper, config, village_ids=None):
    """Top up the coin village. Safe to call every cycle; acts on its own clock.

    Opt-in and never self-starting: with minting.enabled off the module does
    nothing at all, and even then it only asks for resources - it never mints,
    switches on auto-minting, or touches a flag.
    """
    settings = (config or {}).get("minting", {}) or {}
    state = load_state()
    # "Request now" is a one-off the user asked for, so it is served whether or
    # not the recurring run is switched on - the button existing at all is the
    # promise that pressing it does something.
    asked = bool(state.get("run_now"))
    if not settings.get("enabled", False) and not asked:
        return
    village_id = str(settings.get("village") or "").strip()
    if not village_id:
        logger.warning("Minting is on but no coin village is set")
        return

    interval = max(5, int(settings.get("interval_minutes", 60) or 60)) * 60
    now = int(time.time())
    due = (settings.get("enabled", False)
           and now - int(state.get("last_run") or 0) >= interval)
    if not due and not asked:
        return
    state["run_now"] = False

    academy = read_academy(wrapper, village_id)
    if academy:
        state["academy"] = dict(academy, when=now)
    call = read_call(wrapper, village_id, str(settings.get("group", "0") or "0"))
    if call is None:
        logger.warning("Could not read the request screen for village %s",
                       village_id)
        state["last_run"] = now
        save_state(state)
        return

    # What the target can still hold. Anything beyond this is thrown away on
    # arrival, so it is the number the whole plan is built around - and it is
    # the TARGET's warehouse that matters. It used to take the largest warehouse
    # among the sending villages as a stand-in, which is a different village's
    # number and wrong in both directions.
    target = FileManager.load_json_file(
        "cache/managed/%s.json" % village_id) or {}
    storage = int(settings.get("storage", 0) or 0) or int(target.get("storage_max") or 0)
    if not storage:
        # No snapshot of the coin village yet: fall back to the biggest
        # warehouse on the screen rather than planning against nothing.
        for village in call["villages"]:
            storage = max(storage, village.get("storage") or 0)
    held = {r: int((target.get("resources") or {}).get(r) or 0) for r in RESOURCES}
    state["held"] = held
    headroom = max(0, storage * len(RESOURCES)
                   - sum(call["incoming"].values()) - sum(held.values()))

    asks = plan_request(
        call["villages"], headroom,
        (academy or {}).get("cost") or BASE_COIN_COST,
        ratio=str(settings.get("ratio", "coin") or "coin"),
        keep=int(settings.get("keep", 0) or 0),
        minimum=int(settings.get("min_send", 1000) or 1000))

    sent = send_request(wrapper, call["form"], asks) if asks else 0
    total = {r: sum(a["amounts"][r] for a in asks) for r in RESOURCES}
    logger.info("Minting: asked %d village(s) for %s wood, %s stone, %s iron",
                sent, total["wood"], total["stone"], total["iron"])
    state.update({
        "last_run": now,
        "village": village_id,
        "incoming": call["incoming"],
        "headroom": headroom,
        "last_asks": asks[:60],
        "last_total": total,
        "asked_villages": sent,
        "candidates": len(call["villages"]),
    })
    save_state(state)
