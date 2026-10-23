"""Plain-assert checks for the premium exchange watcher:
python3 tests/test_premium_market.py"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import premium_market as pm  # noqa: E402

# A real reading (PremiumExchange.receiveData) whose screen showed a Koers of
# 273 / 273 / 243.
DATA = {
    "stock": {"wood": 278285, "stone": 273651, "iron": 254659},
    "capacity": {"wood": 339707, "stone": 333358, "iron": 322093},
    "rates": {"wood": 0.0035500754780974503, "stone": 0.003538437505306231,
              "iron": 0.003982664947835821},
    "tax": {"buy": 0.03, "sell": 0},
    "constants": {"resource_base_price": 0.015, "resource_price_elasticity": 0.0148,
                  "stock_size_modifier": 20000},
    "duration": 5760, "merchants": 7,
}


def test_koers_matches_the_exchange_screen():
    snap = pm.snapshot(DATA, now=1000)
    assert snap["koers"] == {"wood": 273, "stone": 273, "iron": 243}
    assert snap["room"]["wood"] == 339707 - 278285


def test_the_rate_is_the_games_formula():
    rate = pm.marginal_rate(278285, 339707, DATA["constants"])
    assert abs(rate - DATA["rates"]["wood"]) < 1e-12


def test_a_full_market_has_no_room():
    full = dict(DATA, stock=dict(DATA["capacity"]))
    snap = pm.snapshot(full)
    assert snap["room"] == {"wood": 0, "stone": 0, "iron": 0}
    assert pm.sellable_points(snap, "wood", 5000) == (0, 0)


def test_selling_stops_at_the_limit_and_gets_dearer():
    snap = pm.snapshot(DATA)
    assert pm.sellable_points(snap, "wood", 0) == (0, 0)
    assert pm.sellable_points(snap, "wood", 250) == (0, 0)      # 273 > 250
    few, cost_few = pm.sellable_points(snap, "wood", 280)
    many, cost_many = pm.sellable_points(snap, "wood", 400)
    assert 0 < few < many
    # Each extra point costs more than the one before it.
    assert cost_many / many > cost_few / few


def test_settings_fill_in_and_clamp():
    conf = pm.settings({"premium": {"track_min_minutes": 20, "track_max_minutes": 5,
                                    "max_rate": {"wood": "300"}, "keep_pct": 150}})
    assert conf["track_market"] is False and conf["sell"] is False
    assert conf["track_min_minutes"] == 20 and conf["track_max_minutes"] == 20
    assert conf["max_rate"] == {"wood": 300, "stone": 0, "iron": 0}
    assert conf["keep_pct"] == 100


class Page:
    def __init__(self, text):
        self.text = text


class Wrapper:
    def __init__(self):
        self.reads = 0

    def get_url(self, url):
        self.reads += 1
        import json
        return Page("<script>PremiumExchange.receiveData(%s);</script>" % json.dumps(DATA))


def test_watch_reads_on_its_own_clock_only_when_tracking():
    store = {}
    pm.load_state = lambda: dict(store.get("state") or {})
    pm.save_state = lambda s: store.__setitem__("state", s)
    pm.load_history = lambda: list(store.get("history") or [])
    pm.FileManager.save_json_file_atomic = staticmethod(
        lambda data, path: store.__setitem__("history" if "history" in path else "x", data))
    w = Wrapper()
    off = {"premium": {"track_market": False}}
    assert pm.watch(w, "1", off, now=1000) is None and w.reads == 0
    on = {"premium": {"track_market": True, "track_min_minutes": 13, "track_max_minutes": 19}}
    snap = pm.watch(w, "1", on, now=1000)
    assert snap and w.reads == 1
    gap = store["state"]["next_read"] - 1000
    assert 13 * 60 <= gap <= 19 * 60
    # Not due yet: no second read.
    assert pm.watch(w, "1", on, now=1000 + 60) is None and w.reads == 1
    assert pm.watch(w, "1", on, now=1000 + gap + 1) is not None and w.reads == 2
    assert len(store["history"]) == 2


def test_koers_on_a_full_old_world_matches_the_screen():
    # A full market whose screen showed 2061 / 2065 / 1994: the game's search
    # gives up after 50 steps.
    full = {"stock": {"wood": 1061810, "stone": 1065774, "iron": 1003664},
            "capacity": {"wood": 1061810, "stone": 1065774, "iron": 1003664},
            "rates": {"wood": 0.000473, "stone": 0.000472, "iron": 0.000489},
            "tax": {"buy": 0.03, "sell": 0}, "constants": DATA["constants"]}
    assert pm.snapshot(full)["koers"] == {"wood": 2061, "stone": 2065, "iron": 1994}


# -- selling --------------------------------------------------------------------

def conf(**kw):
    base = {"premium": {"sell": True, "max_rate": {"wood": 300, "stone": 300, "iron": 300},
                        "keep_pct": 50, "max_pp_per_day": 50, "skip_short": True}}
    base["premium"].update(kw)
    return pm.settings(base)


def village(wood, stone, iron, storage=100000, free=10):
    return {"resources": {"wood": wood, "stone": stone, "iron": iron},
            "storage_max": storage, "merchants": {"free": free, "total": 10}}


def test_the_request_rounds_up_to_exactly_the_planned_points():
    # The game sells whole points, rounding a request up.
    snap = pm.snapshot(DATA)
    st, cap, c = snap["stock"]["iron"], snap["capacity"]["iron"], DATA["constants"]
    for want in (1, 2, 10, 37):
        a = pm.amount_for_points(st, cap, c, 0, want, 100000)
        assert math.ceil(pm.points_for(st, cap, c, 0, a)) == want, want


def test_a_budget_too_small_for_the_rounded_points_is_refused():
    snap = pm.snapshot(DATA)
    st, cap, c = snap["stock"]["iron"], snap["capacity"]["iron"], DATA["constants"]
    exact = pm.amount_for_points(st, cap, c, 0, 2, 100000)
    full_two = 0
    while pm.points_for(st, cap, c, 0, full_two) < 2:
        full_two += 1
    assert pm.amount_for_points(st, cap, c, 0, 2, full_two - 1) is None
    assert pm.amount_for_points(st, cap, c, 0, 2, full_two) == exact


def test_plan_sells_the_cheapest_resource_first_from_what_is_spare():
    snap = pm.snapshot(DATA)  # iron 243 is cheapest, wood/clay 273
    sales = pm.plan_sales(snap, conf(), {"a": village(60000, 60000, 60000)})
    assert sales and sales[0]["res"] == "iron"
    # Keeps 50% of a 100k warehouse: at most 10k iron spare, and 10 merchants.
    assert sum(x["amount"] for x in sales if x["res"] == "iron") <= 10000


def test_plan_respects_limit_daily_cap_and_short_resources():
    snap = pm.snapshot(DATA)
    assert pm.plan_sales(snap, conf(max_rate={"wood": 0, "stone": 0, "iron": 0}),
                         {"a": village(90000, 90000, 90000)}) == []
    capped = pm.plan_sales(snap, conf(max_pp_per_day=5), {"a": village(90000, 90000, 90000)})
    assert sum(x["points"] for x in capped) == 5
    assert pm.plan_sales(snap, conf(max_pp_per_day=5), {"a": village(90000, 90000, 90000)},
                         sold_today=5) == []
    # Iron is short in this village: it is not sold, wood/clay are.
    lopsided = pm.plan_sales(snap, conf(), {"a": village(90000, 90000, 10000)})
    assert lopsided and all(x["res"] != "iron" for x in lopsided)


def test_plan_never_sells_into_a_full_market_or_without_merchants():
    full = pm.snapshot(dict(DATA, stock=dict(DATA["capacity"])))
    assert pm.plan_sales(full, conf(), {"a": village(90000, 90000, 90000)}) == []
    snap = pm.snapshot(DATA)
    assert pm.plan_sales(snap, conf(), {"a": village(90000, 90000, 90000, free=0)}) == []


def test_a_village_without_a_merchant_count_still_sells():
    # One-village accounts never get a merchant count in their snapshot.
    snap = pm.snapshot(DATA)   # the reading saw 7 free merchants
    lone = village(90000, 90000, 90000)
    lone["merchants"] = None
    sales = pm.plan_sales(snap, conf(), {"1001": lone}, read_village="1001")
    assert sales and sum(x["amount"] for x in sales) <= 7000
    # Another village, count unknown: planned on what it has spare.
    sales = pm.plan_sales(snap, conf(), {"other": lone}, read_village="1001")
    assert sales


class Exchange:
    """The two ajax steps, answering like the game."""

    def __init__(self, cost=-10.0, error=None, confirm=True):
        self.cost, self.error, self.confirm = cost, error, confirm
        self.calls = []

    def get_api_action(self, village_id, action, params=None, data=None):
        self.calls.append((action, dict(data)))
        if action == "exchange_begin":
            res = [k[5:] for k in data if k.startswith("sell_")][0]
            amount = data["sell_" + res]
            row = {"resource": res, "amount": -amount, "cost": self.cost,
                   "rate_hash": "abc", "merchants_required": -(-amount // 1000)}
            if self.error:
                row["error"] = self.error
            return {"response": [row]}
        return {"response": {"success": self.confirm}}


def test_a_sale_is_quoted_then_confirmed_with_the_games_fields():
    ex = Exchange(cost=-10.0)
    points, error = pm.execute_sale(ex, {"village": "1", "res": "iron", "amount": 2500,
                                         "points": 10}, 300)
    assert (points, error) == (10, None)
    assert ex.calls[1] == ("exchange_confirm", {"rate_iron": "abc", "sell_iron": 2500, "mb": "1"})


def test_a_sale_whose_price_moved_is_not_confirmed():
    ex = Exchange(cost=-5.0)   # 2500 for 5 points = 500 per point, limit 300
    points, error = pm.execute_sale(ex, {"village": "1", "res": "iron", "amount": 2500,
                                         "points": 10}, 300)
    assert points == 0 and "price moved" in error
    assert [c[0] for c in ex.calls] == ["exchange_begin"]


def test_a_refused_quote_is_not_confirmed():
    ex = Exchange(error="Not enough merchants")
    points, error = pm.execute_sale(ex, {"village": "1", "res": "iron", "amount": 2500,
                                         "points": 10}, 300)
    assert points == 0 and error == "Not enough merchants"
    assert len(ex.calls) == 1


def test_a_quote_past_the_village_budget_is_not_confirmed():
    ex = Exchange(cost=-2.0)
    points, error = pm.execute_sale(ex, {"village": "1", "res": "iron", "amount": 475,
                                         "points": 2, "budget": 300}, 300)
    assert points == 0 and "more than" in error and len(ex.calls) == 1


# -- start and stop ---------------------------------------------------------

def test_no_sale_starts_above_the_start_even_with_a_high_stop():
    snap = pm.snapshot(DATA)   # iron Koers 243
    sales = pm.plan_sales(snap, conf(max_rate={"wood": 0, "stone": 0, "iron": 200},
                                     stop_rate={"iron": 400}),
                          {"a": village(90000, 90000, 90000)})
    assert sales == []


def test_a_started_sale_runs_on_to_the_stop():
    snap = pm.snapshot(DATA)
    villages = {"a": village(400000, 400000, 400000, storage=500000, free=200)}
    only_start = pm.plan_sales(snap, conf(max_rate={"wood": 0, "stone": 0, "iron": 250},
                                          max_pp_per_day=10000), villages)
    with_stop = pm.plan_sales(snap, conf(max_rate={"wood": 0, "stone": 0, "iron": 250},
                                         stop_rate={"iron": 300}, max_pp_per_day=10000), villages)
    assert sum(x["points"] for x in with_stop) > sum(x["points"] for x in only_start) > 0


def test_a_stop_of_zero_is_the_start_and_never_below_it():
    c = pm.settings({"premium": {"max_rate": {"iron": 250}, "stop_rate": {"iron": 0, "wood": 500}}})
    assert c["stop_rate"]["iron"] == 250
    assert c["stop_rate"]["wood"] == 0     # wood never starts, so no stop either
    c = pm.settings({"premium": {"max_rate": {"iron": 250}, "stop_rate": {"iron": 100}}})
    assert c["stop_rate"]["iron"] == 250


def test_until_full_sells_the_whole_room():
    snap = pm.snapshot(DATA)
    room = snap["room"]["iron"]
    villages = {"a": village(900000, 900000, 900000, storage=1000000, free=1000)}
    sales = pm.plan_sales(snap, conf(max_rate={"wood": 0, "stone": 0, "iron": 300},
                                     stop_rate={"iron": 99999}, max_pp_per_day=100000), villages)
    sold = sum(x["amount"] for x in sales)
    assert room - 2000 < sold <= room


# -- the market moving between quote and confirmation ----------------------

class MovingExchange(Exchange):
    """Answers the first confirmation with a fresh quote, as the game does
    when somebody else sold in the meantime."""

    def __init__(self, requote_cost, requote_error=None, **kw):
        super().__init__(**kw)
        self.requote_cost, self.requote_error, self.confirms = requote_cost, requote_error, 0

    def get_api_action(self, village_id, action, params=None, data=None):
        if action != "exchange_confirm":
            return super().get_api_action(village_id, action, params, data)
        self.calls.append((action, dict(data)))
        self.confirms += 1
        if self.confirms == 1:
            res = [k[5:] for k in data if k.startswith("sell_")][0]
            return {"response": {"transactions": [{
                "resource": res, "amount": -(data["sell_" + res] + 85),
                "cost": self.requote_cost, "rate_hash": "fresh",
                "error": self.requote_error}]}}
        return {"response": {"success": True}}


def test_a_fresh_quote_that_still_holds_is_confirmed():
    ex = MovingExchange(requote_cost=-10.0, cost=-10.0)
    points, error = pm.execute_sale(ex, {"village": "1", "res": "wood", "amount": 2500,
                                         "points": 10, "budget": 5000}, 300)
    assert (points, error) == (10, None)
    assert ex.calls[-1] == ("exchange_confirm", {"rate_wood": "fresh", "sell_wood": 2585, "mb": "1"})


def test_a_fresh_quote_with_the_games_refusal_ends_the_sale():
    ex = MovingExchange(requote_cost=-9.0, requote_error="De beurs heeft niet genoeg ruimte", cost=-10.0)
    points, error = pm.execute_sale(ex, {"village": "1", "res": "wood", "amount": 2500,
                                         "points": 10}, 300)
    assert points == 0 and "niet genoeg ruimte" in error
    assert ex.confirms == 1


def test_a_fresh_quote_past_the_price_limit_is_not_confirmed():
    ex = MovingExchange(requote_cost=-5.0, cost=-10.0)   # 2585 / 5 = 517 per point
    points, error = pm.execute_sale(ex, {"village": "1", "res": "wood", "amount": 2500,
                                         "points": 10}, 300)
    assert points == 0 and "price moved" in error and ex.confirms == 1


def test_check_now_makes_the_watcher_read_at_once():
    store = {"state": {"next_read": 10 ** 10}}
    pm.load_state = lambda: dict(store.get("state") or {})
    pm.save_state = lambda s: store.__setitem__("state", s)
    pm.load_history = lambda: []
    pm.FileManager.save_json_file_atomic = staticmethod(lambda data, path: None)
    requested = {"on": True}
    pm.FileManager.load_json_file = staticmethod(
        lambda path: {"requested": 1} if path == pm.CHECK_REQUEST and requested["on"] else None)
    pm.FileManager.remove_file = staticmethod(lambda path: requested.__setitem__("on", False))
    w = Wrapper()
    on = {"premium": {"track_market": True}}
    assert pm.watch(w, "1", on, now=1000) is not None and w.reads == 1
    assert requested["on"] is False
    # Served: the clock rules again.
    assert pm.watch(w, "1", on, now=1001) is None and w.reads == 1


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
