"""Plain-assert checks for the flag pass: python3 tests/test_flags.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import flags  # noqa: E402

RES = 1
ATTACK = 3
CDN = "https://cdn.example/asset/x/graphic/flags"

# The flag grid that follows the current-flag block on the real screen, drawn
# with the same <type>_<level> images.
GRID = ('<div id="flags_container"><div class="flag_box" style="background-image: '
        'url(\'%s/medium/1_1@2x.webp\')" id="flag_box_1_1"></div></div>' % CDN)


def screen(current=None, cooldown=False):
    if current:
        block = ('<div id="current_flag" style="margin-top: 10px; "><table><tr><td>'
                 '<img src="%s/medium/%d_%d@2x.webp" /><strong>Name</strong>'
                 '</td></tr></table></div>' % (CDN, current[0], current[1]))
    else:
        block = ('<div id="current_flag" style="margin-top: 10px; display: none">'
                 '<table><tr><td><img src="" /></td></tr></table></div>')
    timer = '<span class="timer cooldown">23:59:00</span>' if cooldown else " "
    return ('<div id="cooldown_container">%s</div><div id="flags_current">%s</div>%s'
            % (timer, block, GRID))


def want(*vids, flag_type=RES, priority=0):
    return {v: {"flag_type": flag_type, "priority": priority} for v in vids}


def read(**carrying):
    """readings: village -> (type, level), None for no flag."""
    return {v: {"flag": list(f) if f else None, "ts": 1} for v, f in carrying.items()}


def targets(outcome):
    return {m["village_id"]: m["level"] for m in outcome["moves"]}


# -- reading the screen -----------------------------------------------------

def test_current_flag_is_read_from_a_webp_image():
    assert flags.parse_current_flag(screen((1, 3))) == [1, 3]


def test_a_village_without_a_flag_does_not_read_the_grid_below():
    assert flags.parse_current_flag(screen(None)) is None


def test_an_old_png_skin_still_reads():
    page = ('<div id="current_flag" style="margin-top: 10px; "><img '
            'src="/graphic/flags/medium/4_2.png" /></div>' + GRID)
    assert flags.parse_current_flag(page) == [4, 2]


def test_cooldown_is_read_from_the_cooldown_box():
    assert flags.parse_cooldown(screen((1, 3), cooldown=True)) is True
    assert flags.parse_cooldown(screen((1, 3))) is False


# -- who gets which level ---------------------------------------------------

def test_the_biggest_village_gets_the_highest_free_level():
    out = flags.allocate(want("a", "b", "c"), {RES: {1: 5, 2: 1, 5: 1}},
                         read(a=None, b=None, c=None),
                         sizes={"a": 1000, "b": 9000, "c": 5000})
    assert targets(out) == {"b": 5, "c": 2, "a": 1}


def test_a_high_flag_on_a_small_village_moves_up():
    # small carries the level 5, big a level 1; one level 2 lies free.
    out = flags.allocate(want("small", "big"), {RES: {2: 1}},
                         read(small=(RES, 5), big=(RES, 1)),
                         sizes={"small": 500, "big": 9000})
    assert targets(out) == {"big": 5, "small": 2}
    # small steps down first: that is what puts the level 5 back in the pool.
    assert [m["village_id"] for m in out["moves"]] == ["small", "big"]
    assert out["waiting"] == []


def test_a_village_already_on_its_level_is_not_touched():
    out = flags.allocate(want("a", "b"), {RES: {1: 3}},
                         read(a=(RES, 4), b=(RES, 2)),
                         sizes={"a": 9000, "b": 1000})
    assert out["moves"] == []


def test_a_flag_hung_by_hand_is_kept():
    out = flags.allocate(want("a", "b"), {RES: {3: 2}},
                         read(a=(ATTACK, 6), b=None), sizes={"a": 9000, "b": 1})
    assert targets(out) == {"b": 3}
    assert [k["village_id"] for k in out["kept"]] == ["a"]


def test_keep_manual_off_replaces_it():
    out = flags.allocate(want("a"), {RES: {3: 1}}, read(a=(ATTACK, 6)),
                         keep_manual=False)
    assert targets(out) == {"a": 3}


def test_an_unread_village_is_left_alone():
    out = flags.allocate(want("a", "b"), {RES: {3: 2}}, read(b=None))
    assert targets(out) == {"b": 3}
    assert out["unread"] == ["a"]


def test_a_village_on_cooldown_is_left_alone():
    readings = read(a=(RES, 1), b=None)
    readings["a"]["cooldown"] = True
    out = flags.allocate(want("a", "b"), {RES: {5: 1}}, readings,
                         sizes={"a": 9000, "b": 1})
    assert targets(out) == {"b": 5}
    assert out["held"] == ["a"]


def test_when_short_nobody_loses_a_flag():
    # Three villages, one standing flag and one free one: the holder keeps its
    # level 1 even though it is the smallest, the biggest gets the free one.
    out = flags.allocate(want("a", "b", "c"), {RES: {4: 1}},
                         read(a=(RES, 1), b=None, c=None),
                         sizes={"a": 10, "b": 9000, "c": 5000})
    assert targets(out) == {"b": 4}
    assert [u["village_id"] for u in out["unmet"]] == ["c"]


def test_a_more_specific_row_is_served_first():
    desired = want("a", priority=0)
    desired.update(want("b", priority=1))
    out = flags.allocate(desired, {RES: {5: 1, 1: 1}}, read(a=None, b=None),
                         sizes={"a": 9000, "b": 10})
    assert targets(out) == {"b": 5, "a": 1}


def test_a_swap_cycle_waits_instead_of_failing():
    # Neither level is free: a needs b's 5 and b needs a's 4 and nothing
    # lies free, so neither move can be the first one.
    out = flags.allocate(want("a", "b"), {}, read(a=(RES, 4), b=(RES, 5)),
                         sizes={"a": 9000, "b": 10})
    assert out["moves"] == []
    assert sorted(m["village_id"] for m in out["waiting"]) == ["a", "b"]


def test_the_free_pool_is_what_the_game_counts():
    # The inventory is free flags only: a standing flag must not be taken off
    # the count a second time.
    out = flags.allocate(want("a", "b"), {RES: {2: 1}},
                         read(a=(RES, 2), b=None), sizes={"a": 9000, "b": 10})
    assert out["moves"] == [{"village_id": "b", "flag_type": RES, "level": 2,
                             "from": None}]


# -- the whole pass, against a fake game ------------------------------------

class FakeGame:
    """Flag screens and assign calls, as the game answers them."""

    def __init__(self, carrying, free):
        self.carrying = dict(carrying)
        self.free = {t: dict(levels) for t, levels in free.items()}
        self.assigned = []
        self.last_h = "h"

    def get_url(self, url):
        vid = url.split("village=")[1].split("&")[0]
        counts = {str(t): {str(l): c for l, c in lv.items()} for t, lv in self.free.items()}
        page = screen(self.carrying.get(vid)) + \
            "<script>FlagsScreen.setFlagCounts(%s);</script>" % flags.json.dumps(counts)

        class Res:
            text = page
        return Res()

    def get_api_action(self, village_id, action, params=None, data=None):
        assert action == "assign_flag"
        t, lvl = int(data["flag_type"]), int(data["level"])
        if self.free.get(t, {}).get(lvl, 0) <= 0:
            return {"error": "no such flag"}
        self.free[t][lvl] -= 1
        old = self.carrying.get(village_id)
        if old:
            self.free.setdefault(old[0], {})
            self.free[old[0]][old[1]] = self.free[old[0]].get(old[1], 0) + 1
        self.carrying[village_id] = (t, lvl)
        self.assigned.append((village_id, t, lvl))
        return {"flags": {str(t): {str(l): c for l, c in lv.items()} for t, lv in self.free.items()}}


def run_pass(game, village_ids, sizes, rows, settings=None):
    saved = {}
    flags.load_plan = lambda path=None: {"rows": rows}
    flags.load_groups = lambda: []
    flags.load_state = lambda path=None: {}
    flags.save_state = lambda state: saved.update(state)
    flags._sizes = lambda ids: sizes
    config = {"flags": dict({"manage": True}, **(settings or {})),
              "villages": {v: {} for v in village_ids}}
    flags.apply_plan(game, village_ids[0], config, village_ids, source="dashboard")
    return saved


def test_apply_swaps_a_high_flag_up_and_keeps_a_hand_hung_one():
    game = FakeGame({"small": (RES, 5), "big": (RES, 1), "op": (ATTACK, 6)},
                    {RES: {2: 3}})
    state = run_pass(game, ["small", "big", "op"],
                     {"small": 100, "big": 9000, "op": 8000},
                     [{"group_id": "0", "flag_type": RES}])
    assert game.carrying == {"small": (RES, 2), "big": (RES, 5), "op": (ATTACK, 6)}
    assert [a[0] for a in game.assigned] == ["small", "big"]
    assert all(r["ok"] for r in state["last_result"])
    assert [k["village_id"] for k in state["kept"]] == ["op"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
