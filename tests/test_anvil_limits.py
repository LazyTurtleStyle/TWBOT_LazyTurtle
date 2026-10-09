"""Plain-assert checks for the anvil's per-item limits: python3 tests/test_anvil_limits.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import events as ev  # noqa: E402

# Four common metals and three rare ones, as the event deals them.
MATERIALS = {str(i): {"rarity": 1 if i <= 4 else 2, "label": "m%d" % i}
             for i in range(1, 8)}
COMBOS = ev._anvil_combos(MATERIALS.keys())
# 84 recipes in id order, cheapest block first; the item behind each is simply
# 5000 + its place in the book, apart from the three this test cares about.
_, PREDICT, _ = ev._anvil_layout(
    MATERIALS, [{"recipe_id": 1000 + i} for i in range(len(COMBOS))], {})
RECIPE_OF = {combo: rid for rid, combo in PREDICT.items()}
BOOSTER, PAKKET, OTHER = 3054, 1006, 3009
ITEM_OF = {rid: 5000 + rid for rid in PREDICT}
ITEM_OF[RECIPE_OF[(2, 3, 4)]] = BOOSTER        # all common metals
ITEM_OF[RECIPE_OF[(6, 7, 7)]] = PAKKET         # three rare ones
ITEM_OF[RECIPE_OF[(1, 1, 1)]] = OTHER
RECIPES = [{"recipe_id": rid, "item": {"item_id": item, "name": "item %d" % item}}
           for rid, item in sorted(ITEM_OF.items())]
KNOWN = {ev.anvil_key(c): RECIPE_OF[c] for c in ((2, 3, 4), (6, 7, 7), (1, 1, 1))}
TARGETS = [PAKKET, BOOSTER]


def plan(stock, made=None, limits=None, known=KNOWN, spare=True):
    return ev.anvil_plan(stock, MATERIALS, RECIPES, known, TARGETS, spare=spare,
                         made=made, limits=limits)


def test_without_a_limit_the_cheap_target_is_made_every_time():
    combo, reason, _ = plan({"2": 1, "3": 1, "4": 1})
    assert (combo, reason) == ((2, 3, 4), "target")
    combo, reason, _ = plan({"2": 1, "3": 1, "4": 1}, made={BOOSTER: 40})
    assert (combo, reason) == ((2, 3, 4), "target")


def test_a_reached_limit_stops_it_as_a_target():
    combo, reason, _ = plan({"2": 1, "3": 1, "4": 1}, made={BOOSTER: 11},
                            limits={BOOSTER: 12})
    assert (combo, reason) == ((2, 3, 4), "target")       # one more to go
    combo, reason, _ = plan({"2": 1, "3": 1, "4": 1}, made={BOOSTER: 12},
                            limits={BOOSTER: 12}, spare=False)
    assert (combo, reason) == (None, "waiting for metals")


def test_and_it_does_not_come_back_as_a_spare_craft():
    # Every combination is known, so nothing is left to discover and the only
    # thing these three metals make is the booster that is already at its limit.
    everything = {ev.anvil_key(c): RECIPE_OF[c] for c in COMBOS}
    combo, reason, _ = plan({"2": 1, "3": 1, "4": 1}, made={BOOSTER: 12},
                            limits={BOOSTER: 12}, known=everything)
    assert (combo, reason) == (None, "waiting for metals")
    # With no limit the same stock would have made it again.
    assert plan({"2": 1, "3": 1, "4": 1}, made={BOOSTER: 12}, known=everything)[0] == (2, 3, 4)


def test_a_limit_of_zero_means_never():
    assert plan({"2": 1, "3": 1, "4": 1}, limits={BOOSTER: 0}, spare=False)[0] is None


def test_metals_freed_by_a_limit_go_to_untried_recipes_first():
    combo, reason, _ = plan({"2": 2, "3": 2, "4": 2}, made={BOOSTER: 12},
                            limits={BOOSTER: 12})
    assert reason == "spare" and combo != (2, 3, 4)
    assert ev.anvil_key(combo) not in KNOWN


def test_spare_metals_are_spread_over_what_is_known():
    # Several known all-common recipes are affordable; the one made least
    # wins. The booster is done, so it is not keeping any tin for itself.
    known = {ev.anvil_key(c): RECIPE_OF[c] for c in COMBOS}
    stock = {"1": 3, "2": 3}
    a, b = ITEM_OF[RECIPE_OF[(1, 1, 1)]], ITEM_OF[RECIPE_OF[(2, 2, 2)]]
    others = {ITEM_OF[RECIPE_OF[c]]: 9 for c in COMBOS}
    done = {BOOSTER: 0}
    assert plan(stock, made={**others, a: 6, b: 2}, limits=done, known=known)[0] == (2, 2, 2)
    assert plan(stock, made={**others, a: 1, b: 2}, limits=done, known=known)[0] == (1, 1, 1)


def test_a_limited_target_holds_no_rare_metals_back():
    # The pakket is done; its silver and gold are free for anything else.
    combo, reason, _ = plan({"6": 1, "7": 2}, made={PAKKET: 3}, limits={PAKKET: 3},
                            known={ev.anvil_key(c): RECIPE_OF[c] for c in COMBOS})
    assert combo is None                    # its own recipe is the only fit here
    combo, reason, _ = plan({"6": 3}, made={PAKKET: 3}, limits={PAKKET: 3})
    assert reason == "spare" and combo == (6, 6, 6)
    # Without the limit the same silver is kept for the pakket.
    assert plan({"6": 3})[0] is None


def test_limits_are_read_from_the_settings():
    read = ev.anvil_limits({"craft_limits": {"3054": 12, "1006": None, "9": "x",
                                             "3002": "0", "7": -1}})
    assert read == {3054: 12, 3002: 0}
    assert ev.anvil_limits({}) == {} and ev.anvil_limits(None) == {}


def test_what_was_made_is_counted_by_item_not_by_name():
    log = [{"item": "Edelbooster", "recipe_id": RECIPE_OF[(2, 3, 4)]},
           {"item": "Edelbooster", "recipe_id": RECIPE_OF[(2, 3, 4)]},
           {"item": "Edelbooster", "item_id": 3002, "recipe_id": None},
           {"item": "?", "recipe_id": None}]
    assert ev.anvil_made({"log": log}, RECIPES) == {BOOSTER: 2, 3002: 1}
    assert ev.anvil_made({}, RECIPES) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
