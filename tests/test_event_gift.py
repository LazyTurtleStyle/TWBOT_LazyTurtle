"""The gift vault event driver against a fake game: python3 tests/test_event_gift.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import events  # noqa: E402


def slot(state="available", item=None):
    return {"item": ({"name": item, "item_id": 1} if item else None), "state": state}


def page(openers=0, can_shuffle=False, slots=None, doublers=0):
    data = {"can_reset": True, "can_shuffle": can_shuffle, "can_premium_open": 1,
            "collection": {"items": slots or [slot() for _ in range(18)], "type": "player"},
            "item_counts": {"4002": 0, "4003": openers, "4004": doublers},
            "costs": {"reset": {"cost": 100}, "open": {"cost": 50}}}
    return ('<script>require([], function (EventScreen) { window.EventGift = new EventScreen(%s); });'
            '</script><h2 class="gift-cycle-time"><span class="icon header time"></span>'
            '<span class="timer" title="x">14:27:10</span></h2>' % json.dumps(data))


class Page:
    def __init__(self, text):
        self.text = text


class Game:
    """Serves the event page and answers start/open like the game."""

    def __init__(self, openers, can_shuffle=False, slots=None):
        self.openers, self.can_shuffle = openers, can_shuffle
        self.slots = slots or [slot() for _ in range(18)]
        self.calls = []
        self.last_h = "h"

    def get_url(self, url):
        return Page(page(self.openers, self.can_shuffle, self.slots))

    def get_api_action(self, village_id, action, params=None, data=None):
        self.calls.append((action, dict(data or {})))
        if action == "start":
            self.can_shuffle = False
            self.slots = [slot() for _ in range(18)]
            return {"response": {"player": {}}}
        if action == "open":
            assert data["free"] == 1, "never pays premium points"
            assert self.openers > 0
            self.openers -= 1
            self.slots[data["slot"]] = slot("collected", "Prize %d" % data["slot"])
            return {"response": {"result": {"item": {"name": "Prize %d" % data["slot"],
                                                     "item_id": 9}, "revealed": []},
                                 "player": {}}}
        raise AssertionError("unexpected action %s" % action)


def test_the_page_is_read():
    snap = events._gift_snapshot({}, events._gift_poll(Game(2), "1", "event_gift"))["gift"]
    assert snap["openers"] == 2 and snap["slots"] == 18 and snap["opened"] == 0
    assert snap["renews_in"] == "14:27:10" and snap["open_cost_pp"] == 50


def test_a_revealed_gift_is_opened_first():
    slots = [slot("collected", "X"), slot(), slot("available", "Known prize"), slot()]
    assert events.gift_pick(slots) == 2
    assert events.gift_pick([slot(), slot()]) == 0
    assert events.gift_pick([slot("collected", "X")]) is None


def test_openers_are_spent_and_nothing_else():
    game = Game(openers=3)
    poll = events._gift_poll(game, "1", "event_gift")
    done = events._gift_play(game, "1", "event_gift", {}, poll)
    assert [d["item"] for d in done] == ["Prize 0", "Prize 1", "Prize 2"]
    assert [c[0] for c in game.calls] == ["open", "open", "open"]
    assert game.openers == 0


def test_without_openers_it_does_nothing():
    game = Game(openers=0)
    poll = events._gift_poll(game, "1", "event_gift")
    assert events._gift_play(game, "1", "event_gift", {}, poll) == []
    assert game.calls == []


def test_a_free_round_is_started_when_offered():
    game = Game(openers=1, can_shuffle=True,
                slots=[slot("collected", "old") for _ in range(18)])
    poll = events._gift_poll(game, "1", "event_gift")
    done = events._gift_play(game, "1", "event_gift", {}, poll)
    assert [c[0] for c in game.calls] == ["start", "open"]
    assert len(done) == 1


def test_wins_are_tallied():
    state = {}
    events._gift_record(state, [{"item": "A", "ts": 1}, {"item": "A", "ts": 2},
                                {"item": "B", "ts": 3}])
    assert state["items_won"] == {"A": 2, "B": 1}
    assert state["totals"]["items"] == 3


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
