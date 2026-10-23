"""
Premium exchange watcher.

The premium exchange is one market per world: it holds a stock of each
resource up to a capacity, and its price moves with how full it is. Selling
resources for premium points is only possible while there is room under the
capacity, and the price is best right after somebody has bought the market
out. Those windows are short - players pile in within minutes, at their busiest
in the morning and when people come home from work - and on an older world the
market sits full most of the day.

A human can not watch for that; the bot can. This module only watches: it
reads the exchange page on a clock of its own (a random 13-19 minutes by
default, active hours only) and keeps the history, so the dashboard's Market
page can show when the windows open on this world and what a sensible price is
before anything is sold. One read covers the whole account - it is the same
market from every village.

Prices, as the game computes them (PremiumExchange.receiveData):
- rate[res] is the marginal price in points per resource,
  base - elasticity * stock / (capacity + stock_size_modifier);
- the "Koers" the exchange screen prints is what one point buys, found by the
  game's own capped search (see koers()), with the 3% buy tax and the price
  rising as the stock drops. Selling a point's worth
  costs about 1 / rate (no sell tax on the worlds seen so far), and each sale
  pushes the stock up and the price down.

Selling (premium.sell, ALPHA) rides on the same reading: right after it, a
resource whose Koers is at or below its start (max_rate) is sold until the
Koers would pass its stop (stop_rate), out of
villages' resources above premium.keep_pct, cheapest resource first, capped at
premium.max_pp_per_day. Every sale is quoted by the game first and only
confirmed when the quote still holds. It never buys.

Settings live in config.json under "premium". Tracking and selling ship off.
"""

import json
import logging
import math
import random
import time

from core.extractors import Extractor
from core.filemanager import FileManager
from core.notification import Notification

logger = logging.getLogger("PremiumMarket")

RESOURCES = ("wood", "stone", "iron")
STATE_FILE = "cache/premium_market.json"
# Written by the dashboard's "Check now" button, removed once served: read the
# exchange on the next tick instead of waiting for the clock.
CHECK_REQUEST = "cache/premium_check.json"
HISTORY_FILE = "cache/premium_market_history.json"
# Two weeks of reads at the fastest sane interval is ~1500 rows; keep a little
# more so a slower clock still shows a fortnight.
HISTORY_DAYS = 14
HISTORY_MAX_ROWS = 2500
# A reading sells from at most this many villages, so a wide window never turns
# into a burst of requests.
MAX_SALES_PER_READ = 4
SALES_KEPT = 200
# A sold point may cost this much more than the Koers limit before the quote
# is refused: the Koers is a buy price (3% tax), and each sale moves the price.
QUOTE_SLACK = 1.06

DEFAULTS = {
    "track_market": False,
    "track_min_minutes": 13,
    "track_max_minutes": 19,
    "sell": False,
    # Start selling a resource only while the exchange's Koers is at or below
    # this many resources per point; 0 never sells that resource.
    "max_rate": {"wood": 0, "stone": 0, "iron": 0},
    # Once started, keep selling until the Koers would pass this; 0 = the same
    # as the start. A high number sells until the market is full.
    "stop_rate": {"wood": 0, "stone": 0, "iron": 0},
    # A village keeps at least this percent of its warehouse of a resource.
    "keep_pct": 50,
    "max_pp_per_day": 50,
    # Never sell a resource a village is short of next to its own other two.
    "skip_short": True,
}


def settings(config):
    """The premium section with every key present and sane."""
    raw = dict((config or {}).get("premium") or {})
    conf = dict(DEFAULTS)
    conf.update({k: v for k, v in raw.items() if k in DEFAULTS})
    rates = dict(DEFAULTS["max_rate"])
    rates.update({r: v for r, v in (raw.get("max_rate") or {}).items() if r in RESOURCES})
    conf["max_rate"] = {r: max(0, int(_num(rates.get(r), 0))) for r in RESOURCES}
    stops = dict(DEFAULTS["stop_rate"])
    stops.update({r: v for r, v in (raw.get("stop_rate") or {}).items() if r in RESOURCES})
    # Never below the start: a stop under it would end every sale before it began.
    conf["stop_rate"] = {r: max(conf["max_rate"][r], int(_num(stops.get(r), 0)))
                         if conf["max_rate"][r] else 0 for r in RESOURCES}
    low = max(1, int(_num(conf["track_min_minutes"], 13)))
    high = max(low, int(_num(conf["track_max_minutes"], 19)))
    conf["track_min_minutes"], conf["track_max_minutes"] = low, high
    conf["keep_pct"] = min(100, max(0, int(_num(conf["keep_pct"], 50))))
    conf["max_pp_per_day"] = max(0, int(_num(conf["max_pp_per_day"], 50)))
    for key in ("track_market", "sell", "skip_short"):
        conf[key] = bool(conf[key])
    return conf


def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# -- prices ---------------------------------------------------------------------

def marginal_rate(stock, capacity, constants):
    """Points per resource at this stock: the game's own formula."""
    c = constants or {}
    return c.get("resource_base_price", 0) - c.get("resource_price_elasticity", 0) \
        * stock / (capacity + c.get("stock_size_modifier", 0))


def koers(stock, capacity, constants, buy_tax):
    """What the exchange screen prints, computed the way the game's own script
    does (PremiumExchange.calculateRateForOnePoint): start at floor(1/price)
    resources, and step down one at a time while buying that many would cost
    more than one point - but never more than 50 steps. On a new world the
    loop finds the true amount; on an old, full one it runs out of steps, which
    is why the screen there shows a little more than a point really buys."""
    rate = marginal_rate(stock, capacity, constants)
    if rate <= 0:
        return None
    tax = 1 + (buy_tax or 0)

    def cost(a):
        return tax * (rate + marginal_rate(stock - a, capacity, constants)) / 2 * a

    amount = int(math.floor(1 / rate))
    steps = 0
    while cost(amount) > 1 and steps < 50:
        steps += 1
        amount -= 1
    return amount


def snapshot(data, now=None):
    """One reading of the exchange, reduced to what is worth keeping."""
    now = int(now or time.time())
    stock = {r: int(data["stock"][r]) for r in RESOURCES}
    capacity = {r: int(data["capacity"][r]) for r in RESOURCES}
    rates = {r: float(data["rates"][r]) for r in RESOURCES}
    tax = data.get("tax") or {}
    return {
        "ts": now,
        "stock": stock,
        "capacity": capacity,
        "room": {r: max(0, capacity[r] - stock[r]) for r in RESOURCES},
        "koers": {r: koers(stock[r], capacity[r], data.get("constants") or {},
                           tax.get("buy", 0)) for r in RESOURCES},
        # What selling one more point's worth costs right now (marginal).
        "sell_cost": {r: (int(math.ceil(1 / (rates[r] * (1 - (tax.get("sell") or 0)))))
                          if rates[r] > 0 else None) for r in RESOURCES},
        "rates": rates,
        "tax": {"buy": tax.get("buy", 0), "sell": tax.get("sell", 0)},
        "constants": data.get("constants") or {},
        "merchants": data.get("merchants"),
    }


def sellable_points(snap, res, max_koers):
    """How many whole points `res` could be sold for before the price passes
    max_koers or the market fills - and what it would cost in resources.

    Walks the game's price curve one point at a time: selling pushes the stock
    up, which lowers the rate, which makes the next point dearer.
    """
    if not max_koers or snap["room"].get(res, 0) <= 0:
        return 0, 0
    stock = snap["stock"][res]
    cap = snap["capacity"][res]
    consts = snap["constants"]
    buy_tax = snap["tax"].get("buy", 0)
    points, spent = 0, 0
    while points < 10000:
        rate = marginal_rate(stock, cap, consts)
        k = koers(stock, cap, consts, buy_tax)
        if k is None or k > max_koers:
            break
        # Resources for one more point at the current (falling) rate.
        need = int(math.ceil(1 / rate)) if rate > 0 else None
        if need is None or stock + need > cap:
            break
        stock += need
        spent += need
        points += 1
    return points, spent


# -- selling -------------------------------------------------------------------

def points_for(stock, capacity, constants, sell_tax, amount):
    """Points the game pays for selling `amount` now (its calculateCost with a
    negative amount: the price falls as the stock rises over the sale)."""
    now = marginal_rate(stock, capacity, constants)
    after = marginal_rate(stock + amount, capacity, constants)
    return (1 + (sell_tax or 0)) * (now + after) * amount / 2


def amount_for_points(stock, capacity, constants, sell_tax, points, budget):
    """What to ask the exchange for so that it sells exactly `points` points,
    or None when that would take more than `budget` (or the room left).

    The game sells whole points only: it rounds the points a request is worth
    UP and resizes the amount to match (asked for 300 iron worth 1.26 points,
    it quoted 475 iron for 2). So the request aims a quarter point short; the
    game's rounding lands it on `points`, and the quote is checked anyway."""
    limit = min(int(budget), capacity - stock)
    if limit <= 0:
        return None
    goal = points - 0.25
    if points_for(stock, capacity, constants, sell_tax, limit) < points:
        # Even the whole budget does not pay for `points`: the game's rounding
        # up would ask for more than the village may give.
        return None
    lo, hi = 1, limit
    while lo < hi:
        mid = (lo + hi) // 2
        if points_for(stock, capacity, constants, sell_tax, mid) >= goal:
            hi = mid
        else:
            lo = mid + 1
    return lo


def short_resources(stock, margin_pct):
    """Resources a village holds too little of next to its own other two - the
    balancer's evening-out judgement."""
    mean = sum(stock.get(r, 0) for r in RESOURCES) / 3.0
    if mean <= 0:
        return set()
    return {r for r in RESOURCES if stock.get(r, 0) < mean * (1 - margin_pct / 100.0)}


def plan_sales(snap, conf, villages, sold_today=0, margin_pct=20, read_village=None):
    """What to sell against this reading: [{village, res, amount, points}].

    Cheapest resource first (fewest resources per point). A resource is only
    sold when its Koers is at or below its start (max_rate); the market then
    takes points while the Koers stays at or below its stop (stop_rate); villages with the
    most to spare supply them, each only what is above keep_pct of its
    warehouse and what its free merchants carry. The simulated stock rises with
    every planned sale, so the next one is priced on the market it leaves.
    """
    remaining = max(0, conf["max_pp_per_day"] - int(sold_today or 0))
    consts = snap.get("constants") or {}
    sell_tax = (snap.get("tax") or {}).get("sell", 0)
    stock = dict(snap["stock"])
    sales = []
    # Merchants a village has left in this plan: one merchant carries 1000 of
    # one resource, and a village selling iron has that many fewer for wood.
    merchants_left = {}
    order = sorted(RESOURCES, key=lambda r: snap["koers"].get(r) or 10 ** 9)
    for res in order:
        start = conf["max_rate"].get(res) or 0
        limit = conf["stop_rate"].get(res) or start
        if not start or remaining <= 0 or len(sales) >= MAX_SALES_PER_READ:
            continue
        # Only a Koers at or below the start opens a sale; once open it runs
        # on until the price would pass the stop.
        if (snap["koers"].get(res) or 10 ** 9) > start:
            continue
        cap = snap["capacity"][res]
        sim = dict(snap, stock=dict(stock, **{res: stock[res]}),
                   room=dict(snap["room"], **{res: max(0, cap - stock[res])}))
        left, _ = sellable_points(sim, res, limit)
        left = min(left, remaining)
        donors = []
        for vid, v in (villages or {}).items():
            have = {r: int((v.get("resources") or {}).get(r, 0) or 0) for r in RESOURCES}
            storage = int(v.get("storage_max") or 0)
            if conf["skip_short"] and res in short_resources(have, margin_pct):
                continue
            free = (v.get("merchants") or {}).get("free")
            if str(vid) == str(read_village) and snap.get("merchants") is not None:
                # The exchange page was read from this village: its merchant
                # count is live, and fresher than any snapshot.
                free = snap["merchants"]
            spare = have[res] - storage * conf["keep_pct"] // 100
            if free is not None:
                free = merchants_left.setdefault(str(vid), int(free))
            if free is None:
                # Not known (a snapshot only carries it once the overview pass
                # has refreshed it - never, on a one-village account). Plan on
                # what is spare; the game's quote refuses a sale its merchants
                # cannot carry.
                budget = spare
            elif free <= 0:
                continue
            else:
                budget = min(spare, free * 1000)
            if budget > 0:
                donors.append((budget, str(vid)))
        for budget, vid in sorted(donors, reverse=True):
            if left <= 0 or len(sales) >= MAX_SALES_PER_READ:
                break
            points = left
            amount = None
            while points > 0:
                amount = amount_for_points(stock[res], cap, consts, sell_tax, points, budget)
                if amount is not None:
                    break
                points -= 1
            if points <= 0 or amount is None:
                continue
            sales.append({"village": vid, "res": res, "amount": amount, "points": points,
                          "budget": budget})
            if vid in merchants_left:
                merchants_left[vid] -= -(-amount // 1000)
            stock[res] += amount
            left -= points
            remaining -= points
    return sales


def _body(result):
    """The payload of an ajax answer, whichever way it is wrapped."""
    if isinstance(result, dict) and "response" in result:
        return result["response"]
    return result


def _check_quote(row, sale, limit):
    """(points, sold, error) for one quoted transaction row."""
    if not row:
        return 0, 0, "no quote from the exchange"
    if row.get("error"):
        return 0, 0, str(row.get("error"))
    points = abs(float(row.get("cost") or 0))
    sold = abs(int(row.get("amount") or 0))
    if points < 1 or not sold:
        return 0, 0, "the quote earns no points"
    if sale.get("budget") and sold > sale["budget"]:
        # The game rounded the sale up to whole points past what this village
        # may give (its keep share, its merchants).
        return 0, 0, "the game wants %d, more than the %d this village may give" % (
            sold, sale["budget"])
    if limit and sold / points > limit * QUOTE_SLACK:
        return 0, 0, "price moved: %d per point" % (sold / points)
    return points, sold, None


def _row_for(rows, res):
    rows = rows if isinstance(rows, list) else []
    return next((q for q in rows if isinstance(q, dict) and q.get("resource") == res), None)


def execute_sale(wrapper, sale, limit):
    """Quote, check, confirm - the game's own two steps. Returns (points, error).

    When the market moves between the quote and the confirmation (someone else
    sold in the meantime) the game does not sell: it answers with a fresh
    quote, the way its own screen asks you again. A fresh quote that still
    passes every check is confirmed once more; one that does not ends the
    sale with the game's own reason.
    """
    res, amount = sale["res"], int(sale["amount"])
    raw = wrapper.get_api_action(
        sale["village"], action="exchange_begin", params={"screen": "market"},
        data={"sell_%s" % res: amount})
    row = _row_for(_body(raw), res)
    if row is None:
        return 0, "no quote from the exchange: %s" % str(raw)[:300]
    points, sold, error = _check_quote(row, sale, limit)
    if error:
        return 0, error
    for attempt in range(2):
        answer = _body(wrapper.get_api_action(
            sale["village"], action="exchange_confirm", params={"screen": "market"},
            data={"rate_%s" % res: row.get("rate_hash"), "sell_%s" % res: sold, "mb": "1"}))
        if isinstance(answer, dict) and answer.get("success") is True:
            return int(points), None
        requote = _row_for((answer or {}).get("transactions")
                           if isinstance(answer, dict) else None, res)
        if requote is None:
            return 0, "not confirmed: %s" % str(answer)[:400]
        if attempt:
            break
        row = requote
        points, sold, error = _check_quote(row, sale, limit)
        if error:
            return 0, "market moved before the confirmation: %s" % error
    return 0, "market kept moving: %s" % str(answer)[:400]


def _today(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _managed_villages():
    out = {}
    for name in FileManager.list_directory("cache/managed", ends_with=".json") or []:
        data = FileManager.load_json_file("cache/managed/%s" % name)
        if data:
            out[name.replace(".json", "")] = data
    return out


def sell(wrapper, snap, conf, config, state, now):
    """Sell into this reading if the settings allow; records every sale."""
    daily = state.setdefault("daily", {})
    today = _today(now)
    for day in list(daily):
        if day != today:
            daily.pop(day, None)
    margin = int(((config or {}).get("balancer") or {}).get("ratio_margin_pct", 20) or 20)
    plan = plan_sales(snap, conf, _managed_villages(), daily.get(today, 0), margin,
                      read_village=state.get("read_village"))
    sales = state.setdefault("sales", [])
    for sale in plan:
        points, error = execute_sale(wrapper, sale, conf["stop_rate"].get(sale["res"]))
        entry = dict(sale, ts=int(time.time()), ok=bool(points), points=points or 0,
                     planned=sale["points"], error=error)
        sales.append(entry)
        if points:
            daily[today] = daily.get(today, 0) + points
            logger.info("Sold %d %s for %d premium points from village %s",
                        sale["amount"], sale["res"], points, sale["village"])
            Notification.send("TWB premium: sold %d %s for %d points (%d per point)" % (
                sale["amount"], sale["res"], points, sale["amount"] // max(1, points)))
        else:
            logger.warning("Premium sale of %d %s from %s not done: %s",
                           sale["amount"], sale["res"], sale["village"], error)
            # The market moved or refused: the rest of this plan was priced on
            # the same reading, so leave it for the next one.
            break
    del sales[:-SALES_KEPT]
    return plan


# -- state and history -------------------------------------------------------

def load_state():
    return FileManager.load_json_file(STATE_FILE) or {}


def save_state(state):
    FileManager.save_json_file_atomic(state, STATE_FILE)


def load_history():
    data = FileManager.load_json_file(HISTORY_FILE)
    return data if isinstance(data, list) else []


def _append_history(snap):
    keep_after = snap["ts"] - HISTORY_DAYS * 86400
    rows = [r for r in load_history() if int(r.get("ts", 0)) >= keep_after]
    rows.append({"ts": snap["ts"], "stock": snap["stock"], "capacity": snap["capacity"],
                 "koers": snap["koers"]})
    FileManager.save_json_file_atomic(rows[-HISTORY_MAX_ROWS:], HISTORY_FILE)


def check_requested():
    return bool(FileManager.load_json_file(CHECK_REQUEST))


def due(state, conf, now=None):
    """Whether the watcher should read the exchange now: the clock says so, or
    someone pressed Check now."""
    now = now or time.time()
    return now >= int(state.get("next_read") or 0) or check_requested()


def watch(wrapper, village_id, config, now=None):
    """Read the exchange if tracking or selling is on and the clock says so,
    and sell into the reading when selling is on.

    Returns the new snapshot, or None when nothing was read. The next read is
    scheduled a random track_min..track_max minutes out, so the reads never
    fall into a fixed rhythm.
    """
    conf = settings(config)
    if not (conf["track_market"] or conf["sell"]) or not village_id:
        return None
    state = load_state()
    now = now or time.time()
    if not due(state, conf, now):
        return None
    gap = random.uniform(conf["track_min_minutes"], conf["track_max_minutes"]) * 60
    if check_requested():
        FileManager.remove_file(CHECK_REQUEST)
    page = wrapper.get_url("game.php?village=%s&screen=market&mode=exchange" % village_id)
    data = Extractor.premium_data(getattr(page, "text", "") or "")
    state["next_read"] = int(now + gap)
    if not data or not all(k in data for k in ("stock", "capacity", "rates")):
        state["last_error"] = int(now)
        save_state(state)
        logger.warning("Could not read the premium exchange; next try in %d min",
                       gap // 60)
        return None
    snap = snapshot(data, now)
    state["last"] = snap
    state["last_read"] = int(now)
    state["read_village"] = str(village_id)
    state.pop("last_error", None)
    save_state(state)
    _append_history(snap)
    if conf["sell"] and any(snap["room"].values()):
        try:
            sell(wrapper, snap, conf, config, state, now)
        finally:
            save_state(state)
    logger.info("Premium exchange: %s", ", ".join(
        "%s %s per point (%s)" % (r, snap["koers"][r],
                                  "full" if not snap["room"][r]
                                  else "%dk room" % (snap["room"][r] // 1000))
        for r in RESOURCES))
    return snap
