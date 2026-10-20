"""Plain-assert checks for the premium exchange watcher:
python3 tests/test_premium_market.py"""
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


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
