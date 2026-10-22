"""Plain-assert checks for unit-template stage handling: python3 tests/test_unit_stages.py"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game.troopmanager import TroopManager  # noqa: E402

logging.disable(logging.CRITICAL)

# Same shape as templates/troops/opening_into_off.txt (trimmed): spear stages,
# then the LC unlock at stable:3, then the axe unlock at barracks:8.
TEMPLATE = [
    {"building": "barracks", "level": 1,
     "build": {"barracks": {"spear": 50}}},
    {"building": "barracks", "level": 3,
     "build": {"barracks": {"spear": 150}}},
    {"building": "stable", "level": 3,
     "upgrades": {"light": 1, "spy": 1},
     "build": {"barracks": {"spear": 500}, "stable": {"light": 50, "spy": 40}}},
    {"building": "barracks", "level": 8,
     "upgrades": {"axe": 1},
     "build": {"barracks": {"axe": 400, "spear": 500}, "stable": {"light": 150, "spy": 50}}},
]


def make_manager():
    tm = TroopManager.__new__(TroopManager)
    tm.logger = logging.getLogger("test")
    tm.template = TEMPLATE
    tm.wanted_levels = {}
    tm._research_wait = 0
    return tm


def test_build_targets_accumulate_across_stages():
    tm = make_manager()
    # Barracks 8 reached while stable 3 was passed earlier: the LC stage quota
    # must survive the advance instead of being dropped (uk91 regression).
    entry = tm.get_template_action({"barracks": 8, "stable": 3})
    assert entry["build"] == {
        "barracks": {"spear": 500, "axe": 400},
        "stable": {"light": 150, "spy": 50},
    }
    assert tm.wanted_levels == {"light": 1, "spy": 1, "axe": 1}


def test_stage_gates_still_stop_accumulation():
    tm = make_manager()
    # Barracks 8 not reached yet (axe stage closed), stable 3 passed: LC quota
    # stays at stage-3 values and no axe upgrade is wanted.
    entry = tm.get_template_action({"barracks": 7, "stable": 3})
    assert entry["build"] == {
        "barracks": {"spear": 500},
        "stable": {"light": 50, "spy": 40},
    }
    assert tm.wanted_levels == {"light": 1, "spy": 1}


def test_research_breaks_on_resource_shortfall_no_fallthrough():
    tm = make_manager()
    # light cannot be afforded; axe (later stage, cheaper) must NOT be
    # researched in its place. Buildings should be held: cost fits storage.
    tm.game_data = {"village": {"wood": 300, "stone": 200, "iron": 200}}
    requests = {}
    tm.resman = type("R", (), {"storage": 9000, "request": lambda s, source, resource, amount: requests.__setitem__(resource, amount)})()
    started = []
    tm.wrapper = type("W", (), {})()
    tm.wrapper.get_action = lambda village_id, action: {"fake": "smith"}
    tm.village_id = "2535"
    tm.wanted_levels = {"light": 1, "spy": 1, "axe": 1}
    import game.troopmanager as tmod
    real_smith, real_research = tmod.Extractor.smith_data, tm.attempt_research
    tmod.Extractor.smith_data = staticmethod(lambda result: {
        "available": {
            "light": {"level": 0, "can_research": False, "research_error": True,
                      "wood": 1612, "stone": 400, "iron": 400},
            "spy": {"level": 1},
            "axe": {"level": 0, "can_research": True, "research_error": False},
        }
    })
    tm.attempt_research = lambda unit_type, smith_data=None: started.append(unit_type) or True
    try:
        status = tm.attempt_upgrade()
    finally:
        tmod.Extractor.smith_data = staticmethod(real_smith)
    assert started == [], "later-stage unit must not be researched past a blocked earlier one"
    assert requests == {"wood": 1312, "stone": 200, "iron": 200}
    assert status == {"researched": False, "hold": True}


def test_research_never_holds_when_cost_exceeds_storage():
    tm = make_manager()
    # Cost > storage: only a storage upgrade (a building!) unblocks it.
    tm.game_data = {"village": {"wood": 0, "stone": 0, "iron": 0}}
    tm.resman = type("R", (), {"storage": 1000, "request": lambda s, source, resource, amount: None})()
    tm.wrapper = type("W", (), {})()
    tm.wrapper.get_action = lambda village_id, action: {"fake": "smith"}
    tm.village_id = "2535"
    tm.wanted_levels = {"light": 1, "spy": 1, "axe": 1}
    import game.troopmanager as tmod
    real_smith = tmod.Extractor.smith_data
    tmod.Extractor.smith_data = staticmethod(lambda result: {
        "available": {
            "light": {"level": 0, "can_research": False, "research_error": True,
                      "wood": 1612, "stone": 0, "iron": 0},
            "axe": {"level": 0, "can_research": True, "research_error": False},
        }
    })
    try:
        status = tm.attempt_upgrade()
    finally:
        tmod.Extractor.smith_data = staticmethod(real_smith)
    assert status == {"researched": False, "hold": False}


def test_research_never_holds_on_building_requirement():
    tm = make_manager()
    # Smith level too low for light: the builder is the fix, never a hold.
    tm.game_data = {"village": {"wood": 0, "stone": 0, "iron": 0}}
    tm.resman = type("R", (), {"storage": 9000, "request": lambda s, source, resource, amount: None})()
    tm.wrapper = type("W", (), {})()
    tm.wrapper.get_action = lambda village_id, action: {"fake": "smith"}
    tm.village_id = "2535"
    tm.wanted_levels = {"light": 1, "spy": 1, "axe": 1}
    import game.troopmanager as tmod
    real_smith = tmod.Extractor.smith_data
    tmod.Extractor.smith_data = staticmethod(lambda result: {
        "available": {
            "light": {"level": 0, "can_research": False, "research_error": False,
                      "error_buildings": True, "wood": 10, "stone": 10, "iron": 10},
            "axe": {"level": 0, "can_research": True, "research_error": False},
        }
    })
    try:
        status = tm.attempt_upgrade()
    finally:
        tmod.Extractor.smith_data = staticmethod(real_smith)
    assert status == {"researched": False, "hold": False}


def test_research_in_progress_does_not_hold():
    tm = make_manager()
    tm._research_wait = time.time() + 3600
    status = tm.attempt_upgrade()
    assert status == {"researched": False, "hold": False}


def test_affordable_unlock_researches_first_stage_first():
    tm = make_manager()
    tm.game_data = {"village": {"wood": 99999, "stone": 99999, "iron": 99999}}
    tm.resman = type("R", (), {"storage": 9000, "request": lambda s, source, resource, amount: None})()
    tm.wrapper = type("W", (), {})()
    tm.wrapper.get_action = lambda village_id, action: {"fake": "smith"}
    tm.village_id = "2535"
    tm.wanted_levels = {"light": 1, "spy": 1, "axe": 1}
    import game.troopmanager as tmod
    real_smith = tmod.Extractor.smith_data
    tmod.Extractor.smith_data = staticmethod(lambda result: {
        "available": {
            "light": {"level": 0, "can_research": True, "research_error": False},
            "spy": {"level": 1},
            "axe": {"level": 0, "can_research": True, "research_error": False},
        }
    })
    started = []
    tm.attempt_research = lambda unit_type, smith_data=None: started.append(unit_type) or True
    tm.wrapper.reporter = type("Rep", (), {"report": lambda s, *a, **k: None})()
    try:
        status = tm.attempt_upgrade()
    finally:
        tmod.Extractor.smith_data = staticmethod(real_smith)
    assert started == ["light"], "earliest pending stage must be researched first"
    assert status == {"researched": True, "hold": False}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
