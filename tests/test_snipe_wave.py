"""Plain-assert checks for game/snipe_wave.py: python3 tests/test_snipe_wave.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import snipe_wave as w  # noqa: E402

NOW = 1_000_000.0
T = (NOW + 4 * 3600) * 1000


def c(cid, tid, ms, label, xy=(500, 500)):
    return {"command_id": cid, "target_id": tid, "arrival_ms": int(T + ms),
            "game_label": label, "target_coords": list(xy)}


INC = [c("r1", "A", 0, "Ram"), c("n1", "A", 100, "Edel >>>> SNIPE THIS <<<<"),
       c("n2", "A", 200, "Edel >>>> SNIPE THIS <<<<"), c("n3", "A", 300, "Edel"),
       c("n4", "B", 5000, "Edel", (510, 500)), c("ax", "B", 5050, "Bijl", (510, 500)),
       c("n5", "B", 5075, "Edel", (510, 500)),
       c("n6", "C", 9000, "Edel >>>> C-SNIPE THIS <<<<", (520, 500))]
OWN = {"V1": {"name": "V1", "location": [505, 505]},
       "V2": {"name": "V2", "location": [495, 495]},
       "A": {"name": "A", "location": [500, 500]},
       "V3": {"name": "V3", "location": [530, 530]}}
TROOPS = {"V1": {"spear": 1500, "sword": 600, "heavy": 400, "catapult": 5},
          "V2": {"spear": 900, "sword": 0}, "A": {"spear": 5000, "sword": 5000},
          "V3": {"heavy": 200}}
SPEEDS = (1.0, 1.0, {"spear": 18, "sword": 22, "catapult": 30, "heavy": 11})


def test_trains_and_windows():
    st = dict(w.DEFAULTS)
    g = w.trains(INC)
    assert [len(x) for x in g["A"]] == [3] and [len(x) for x in g["B"]] == [1, 1]
    aim, keep, lo, hi = w.window(INC, "A", int(T + 100), st)
    assert (lo, hi, keep, aim) == (int(T + 1), int(T + 99), 49, int(T + 50))
    assert w.window(INC, "B", int(T + 5075), st)[1] == 11   # axe 25ms before
    assert w.window(INC, "B", int(T + 5000), st)[1] == 49   # nothing close before


def test_coverage():
    st = dict(w.DEFAULTS)
    g = w.trains(INC)["A"]
    assert w.covered(g, {int(T + 100): 1}, st) == {int(T + 100)}
    assert len(w.covered(g, {int(T + 200): 1}, st)) == 3
    assert len(w.covered(g, {int(T + 100): 1}, dict(st, prefer_behind_first=False))) == 3
    assert not w.covered(g, {int(T + 200): 1}, dict(st, keep_hits=2))


def test_plan():
    st = dict(w.DEFAULTS)
    e1 = w.plan(INC, [], OWN, TROOPS, SPEEDS, st, NOW)
    assert e1 and all(e["village_id"] not in ("A", "V3") for e in e1)
    assert all(e["incoming_id"] != "n6" for e in e1)
    assert e1[0]["incoming_id"] == "n2"     # tagged train, behind the first noble
    sends = sorted(e["send_est_ts"] for e in e1)
    assert all(b - a >= 20 for a, b in zip(sends, sends[1:]))
    for v in ("V1", "V2"):
        for pool in ("inf", "hc"):
            t = sorted(e["send_est_ts"] for e in e1
                       if e["village_id"] == v and w._pools(e["units"]) == pool)
            assert all(b - a >= 120 for a, b in zip(t, t[1:]))
    assert all(sum(n for u, n in e["units"].items() if u != "catapult") <= 2000
               and e["units"].get("sword", 0) <= 1000 for e in e1)
    e2 = w.plan(INC, [], OWN, TROOPS, SPEEDS, dict(st, include_untagged=False), NOW)
    assert {e["incoming_id"] for e in e2} <= {"n1", "n2", "n3"}
    e3 = w.plan(INC, [], OWN, TROOPS, SPEEDS, dict(st, skip_nobled=False), NOW)
    assert any(e["village_id"] == "A" and e["target_village_id"] != "A" for e in e3)


def test_cancellations():
    st = dict(w.DEFAULTS)
    armed = w.plan(INC, [], OWN, TROOPS, SPEEDS, st, NOW)
    kept = dict(armed[0], status="done", result="support lands",
                units_sent=armed[0]["units"], id="K", hit_ms=int(T + 200))
    can = dict(w.cancellations(INC, armed + [kept], st))
    assert all(can.get(e["id"]) == "covered" for e in armed if e["target_village_id"] == "A")
    pool = w._pools(kept["units_sent"])
    assert all(can.get(e["id"]) for e in armed
               if e["village_id"] == kept["village_id"] and w._pools(e["units"]) == pool)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
