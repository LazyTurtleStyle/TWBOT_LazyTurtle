"""The wall model against what the game itself says: python3 tests/test_simulator_wall.py

Every expected number here is an in-game result - the battle simulator on the
rally point, or a real attack report - not something worked out by hand.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game.barbshaper import BarbShaper  # noqa: E402
from game.simulator import Simulator, wall_base_defense, wall_bonus  # noqa: E402

SIM = Simulator()


def rounded(attack, wall, luck):
    """Whole troops lost, the way the game reports them."""
    during = SIM.pre_wall(num_rams=attack.get("ram", 0), wall=wall)
    losses = SIM.simulate_against_wall(attack, during, luck=luck)
    out = {}
    for unit, value in losses.items():
        whole = int(math.floor(value + 0.5))
        if whole:
            out[unit] = whole
    return out


def test_base_defense_matches_the_wall_screen():
    # Read off the game's wall screen for villages at these levels.
    for level, shown in ((1, 97), (2, 194), (6, 459), (7, 512), (10, 656), (11, 699),
                         (13, 783), (14, 822), (15, 861), (16, 898), (18, 971),
                         (19, 1006)):
        assert wall_base_defense(level) == shown, level
    assert wall_base_defense(0) == 20


def test_wall_bonus_matches_the_wall_screen():
    for level, pct in ((1, 4), (2, 8), (6, 24), (7, 29), (10, 44), (14, 66),
                       (16, 79), (19, 99)):
        assert round((wall_bonus(level) - 1) * 100) == pct, level


def test_rams_never_take_the_wall_below_half_rounded_down():
    # (wall, rams) -> level the battle was fought at, from the simulator.
    for (wall, rams), during in {
            (1, 2): 1, (1, 4): 0, (2, 2): 2, (2, 4): 1, (2, 256): 1,
            (3, 4): 2, (3, 8): 1, (5, 4): 4, (5, 16): 2, (10, 8): 9,
            (10, 16): 8, (10, 32): 7, (10, 64): 5, (15, 64): 11, (15, 128): 7,
            (20, 64): 17, (20, 128): 14, (20, 256): 10}.items():
        assert SIM.pre_wall(num_rams=rams, wall=wall) == during, (wall, rams)


def test_real_shaper_reports():
    # Five live barb-shaper hits: luck and losses as the reports gave them.
    hit = {"axe": 20, "spy": 1, "ram": 6}
    assert rounded(hit, 2, -20.2) == {"axe": 1}
    assert rounded(hit, 2, 1.2) == {"axe": 1}
    small = {"axe": 20, "spy": 1, "ram": 3}
    for luck in (-18.4, -7.7, 8.7):
        assert rounded(small, 1, luck) == {}


def test_every_unit_type_loses_the_same_share():
    # The cases the old per-attack-type split got wrong.
    assert rounded({"light": 10, "ram": 6, "spy": 1}, 15, 0) == {"light": 5, "ram": 3}
    assert rounded({"light": 20, "ram": 30, "spy": 1}, 15, -5) == {"light": 3, "ram": 5}
    assert rounded({"light": 5, "ram": 12, "spy": 1}, 2, 15) == {"ram": 1}
    assert rounded({"light": 10, "ram": 60, "spy": 1}, 3, -15) == {"ram": 1}
    assert rounded({"light": 80, "ram": 60, "spy": 1}, 1, 15) == {}
    assert rounded({"axe": 50, "light": 20, "ram": 12, "spy": 1}, 10, 0) == \
        {"axe": 2, "light": 1, "ram": 1}
    assert rounded({"axe": 50, "light": 20, "ram": 30, "spy": 1}, 10, -5) == \
        {"axe": 2, "light": 1, "ram": 1}
    assert rounded({"axe": 50, "light": 20, "ram": 3, "spy": 1}, 6, 5) == \
        {"axe": 1, "light": 1}


def test_a_shaper_escort_grows_with_the_wall():
    small = BarbShaper.axes_needed(3, 1, 1.0, 10000)
    bigger = BarbShaper.axes_needed(6, 2, 1.0, 10000)
    assert small is not None and bigger is not None and bigger > small


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
