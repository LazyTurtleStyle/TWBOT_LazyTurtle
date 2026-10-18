"""Plain-assert checks for evening out wood/clay/iron: python3 tests/test_balancer_ratio.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import balancer as balancer_module  # noqa: E402
from game.balancer import NO_MARKET, ResourceBalancer  # noqa: E402


def res(wood, stone, iron):
    return {"wood": wood, "stone": stone, "iron": iron}


def make(margin=20, fill=90):
    b = ResourceBalancer(None, "1")
    b.ratio_balance = True
    b.ratio_margin_pct = margin
    b.target_fill_pct = fill
    return b


# -- what a village has too much and too little of --------------------------

def test_a_def_village_drowning_in_iron_and_wood_has_both_spare():
    # 253k wood, almost no clay, 242k iron: average 165k.
    got = make().ratio_surplus(res(253000, 200, 242000))
    assert set(got) == {"wood", "iron"}
    mean = (253000 + 200 + 242000) / 3.0
    assert got["wood"] == int(253000 - mean)
    assert got["iron"] == int(242000 - mean)


def test_a_small_even_village_has_nothing_spare():
    assert make().ratio_surplus(res(4000, 4100, 3900)) == {}


def test_only_the_part_above_the_average_goes():
    b = make()
    got = b.ratio_surplus(res(30000, 10000, 10000))
    left = 30000 - got["wood"]
    assert left >= (30000 + 10000 + 10000) / 3.0 - 1


def test_the_queue_shortfall_is_held_back():
    b = make()
    b.reserve = {"wood": 25000}
    got = b.ratio_surplus(res(30000, 10000, 10000))
    assert got["wood"] == 5000


def test_an_off_village_starved_of_iron_is_short_of_iron_only():
    got = make().ratio_shortfall(res(121000, 203000, 25000), storage_max=215219)
    assert set(got) == {"iron"}
    assert got["iron"] == int((121000 + 203000 + 25000) / 3.0 - 25000)


def test_what_is_on_its_way_counts_as_delivered():
    b = make()
    assert b.ratio_shortfall(res(121000, 203000, 25000), {"iron": 100000},
                             storage_max=215219) == {}


def test_the_warehouse_ceiling_caps_a_shortfall():
    # Average 6333, so iron is 5333 short - but filled to 50% of a 10k
    # warehouse it may only rise from 1000 to 5000.
    got = make(fill=50).ratio_shortfall(res(9000, 9000, 1000), storage_max=10000)
    assert got == {"iron": 4000}


def test_a_village_levelled_to_its_average_neither_sends_nor_receives():
    b = make()
    stock = res(60000, 50000, 40000)  # 20% band around 50k
    assert b.ratio_surplus(stock) == {}
    assert b.ratio_shortfall(stock, storage_max=400000) == {}


# -- fitting it into merchants ----------------------------------------------

def test_a_swap_only_carries_what_both_sides_agree_on():
    b = make()
    plan = b.plan_ratio({"iron": 91000, "wood": 3000}, {"iron": 77000, "stone": 9000},
                        merchants=50)
    assert plan == {"iron": 50000}


def test_merchants_cap_the_send():
    plan = make().plan_ratio({"iron": 9000}, {"iron": 9000}, merchants=4)
    assert plan == {"iron": 4000}


# -- a whole pass -------------------------------------------------------------

class Reporter:
    def report(self, *args):
        pass


class Wrapper:
    reporter = Reporter()


def test_the_pass_sends_iron_to_the_village_short_of_it_and_nothing_else():
    villages = {
        "1": {"public": {"points": 9000, "location": [500, 500]},
              "resources": res(253000, 200, 242000), "storage_max": 400000},
        "off": {"public": {"points": 4000, "location": [503, 500]},
                "resources": res(121000, 203000, 25000), "storage_max": 215219},
        "even": {"public": {"points": 5000, "location": [501, 500]},
                 "resources": res(50000, 50000, 50000), "storage_max": 215219},
    }
    b = make()
    b.wrapper = Wrapper()
    b.min_send_amount = 250
    sent = []
    b.read_receiver = lambda vid: {"resources": villages[vid]["resources"],
                                   "incoming": {}, "storage_max": villages[vid]["storage_max"]}
    b.send = lambda target, plan: sent.append(dict(plan)) or True
    b._book_send = lambda *a: None
    b._no_market_until = staticmethod(lambda vid: 0)
    balancer_module._load_state = lambda: {}
    stock = dict(villages["1"]["resources"])
    count = b._ratio_pass(villages, stock, merchants=10)
    assert count == 1
    assert sent == [{"iron": 10000}]
    assert stock["iron"] == 232000


def test_a_receiver_without_a_market_is_skipped():
    villages = {
        "1": {"public": {"points": 9000}, "resources": res(253000, 200, 242000),
              "storage_max": 400000},
        "off": {"public": {"points": 4000}, "resources": res(121000, 203000, 25000),
                "storage_max": 215219},
    }
    b = make()
    b.wrapper = Wrapper()
    marked = []
    b.read_receiver = lambda vid: NO_MARKET
    b._mark_no_market = lambda vid: marked.append(vid) or True
    b._no_market_until = staticmethod(lambda vid: 0)
    b.send = lambda target, plan: (_ for _ in ()).throw(AssertionError("sent"))
    balancer_module._load_state = lambda: {}
    assert b._ratio_pass(villages, dict(villages["1"]["resources"]), merchants=10) == 0
    assert marked == ["off"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
