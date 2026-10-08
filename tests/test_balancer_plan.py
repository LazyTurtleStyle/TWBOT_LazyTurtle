"""Plain-assert checks for the balancer's send plan: python3 tests/test_balancer_plan.py"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game.balancer import MERCHANT_CAPACITY, RESOURCES, ResourceBalancer  # noqa: E402

CEILING = 160000


def plan(have, stock, merchants=500, ceiling=CEILING):
    """Plan a send to a receiver holding `have`, from a sender holding `stock`."""
    b = ResourceBalancer(None, "1")
    room = {r: ceiling - have[r] for r in RESOURCES if ceiling > have[r]}
    return b._plan(room, stock, merchants), b


def res(wood, stone, iron):
    return {"wood": wood, "stone": stone, "iron": iron}


def test_fullest_resource_is_not_topped_up_alone():
    # The live case: a sender with only stone to spare, a receiver already
    # holding more stone than anything else.
    got, _ = plan(res(15004, 48539, 12286), res(0, 90000, 0))
    assert got == {}


def test_a_resource_rises_only_as_far_as_the_fullest():
    got, _ = plan(res(15004, 48539, 12286), res(90000, 0, 0))
    assert got == {"wood": 48539 - 15004}


def test_two_resources_may_rise_together():
    got, _ = plan(res(15004, 48539, 12286), res(60000, 60000, 0))
    # Wood can reach 75004, so stone stops there too rather than at 108539.
    assert got == {"wood": 60000, "stone": 75004 - 48539}


def test_flat_receiver_still_gets_everything():
    got, _ = plan(res(20000, 20000, 20000), res(50000, 50000, 50000))
    assert got == res(50000, 50000, 50000)
    # ... but one resource on its own would only make it lopsided.
    got, _ = plan(res(20000, 20000, 20000), res(50000, 0, 0))
    assert got == {}


def test_a_resource_at_the_ceiling_lets_the_others_catch_up():
    got, _ = plan(res(10000, CEILING + 500, 10000), res(200000, 200000, 0))
    assert got == {"wood": CEILING - 10000}


def test_merchants_still_cap_the_plan():
    got, b = plan(res(1000, 60000, 1000), res(90000, 90000, 90000), merchants=20)
    assert b.merchants_for(got) <= 20
    assert "stone" not in got and got["wood"] == got["iron"] == 10000


def test_biggest_gap_mode_is_untouched():
    b = ResourceBalancer(None, "1")
    b.fill_mode = "biggest_gap"
    room = {"wood": 100000, "stone": 50000, "iron": 100000}
    assert b._plan(room, res(0, 40000, 0), 500) == {"stone": 40000}


def test_fuzz():
    rng = random.Random(7)
    for _ in range(20000):
        have = {r: rng.choice((0, rng.randint(0, CEILING + 20000))) for r in RESOURCES}
        stock = {r: rng.choice((0, rng.randint(0, 400000))) for r in RESOURCES}
        merchants = rng.randint(1, 400)
        got, b = plan(have, stock, merchants)
        assert b.merchants_for(got) <= merchants
        after = dict(have)
        for r, amount in got.items():
            assert b.min_send_amount <= amount <= stock[r]
            after[r] += amount
            assert after[r] <= CEILING
        # What each resource could have been raised to, sender and receiver
        # permitting. Nothing may end up above the best of the other two.
        could = {r: max(have[r], min(CEILING, have[r] + stock[r])) for r in RESOURCES}
        for r in got:
            assert after[r] <= max(could[s] for s in RESOURCES if s != r), (have, stock, got)
        # Whatever ends up fullest either stood there already or has company:
        # a second resource within the part-loads that merchant rounding costs.
        top = max(after.values())
        if top > max(have.values()):
            near = [r for r in RESOURCES if after[r] >= top - 2 * MERCHANT_CAPACITY]
            assert len(near) >= 2, (have, stock, merchants, got)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
