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

Settings live in config.json under "premium". Tracking and selling ship off.
"""

import json
import logging
import math
import random
import time

from core.extractors import Extractor
from core.filemanager import FileManager

logger = logging.getLogger("PremiumMarket")

RESOURCES = ("wood", "stone", "iron")
STATE_FILE = "cache/premium_market.json"
HISTORY_FILE = "cache/premium_market_history.json"
# Two weeks of reads at the fastest sane interval is ~1500 rows; keep a little
# more so a slower clock still shows a fortnight.
HISTORY_DAYS = 14
HISTORY_MAX_ROWS = 2500

DEFAULTS = {
    "track_market": False,
    "track_min_minutes": 13,
    "track_max_minutes": 19,
    "sell": False,
    # Sell only while the exchange's Koers is at or below this many resources
    # per point; 0 never sells that resource.
    "max_rate": {"wood": 0, "stone": 0, "iron": 0},
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


def due(state, conf, now=None):
    """Whether the watcher should read the exchange now."""
    now = now or time.time()
    return now >= int(state.get("next_read") or 0)


def watch(wrapper, village_id, config, now=None):
    """Read the exchange if tracking is on and the clock says so.

    Returns the new snapshot, or None when nothing was read. The next read is
    scheduled a random track_min..track_max minutes out, so the reads never
    fall into a fixed rhythm.
    """
    conf = settings(config)
    if not conf["track_market"] or not village_id:
        return None
    state = load_state()
    now = now or time.time()
    if not due(state, conf, now):
        return None
    gap = random.uniform(conf["track_min_minutes"], conf["track_max_minutes"]) * 60
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
    state.pop("last_error", None)
    save_state(state)
    _append_history(snap)
    logger.info("Premium exchange: %s", ", ".join(
        "%s %s per point (%s)" % (r, snap["koers"][r],
                                  "full" if not snap["room"][r]
                                  else "%dk room" % (snap["room"][r] // 1000))
        for r in RESOURCES))
    return snap
