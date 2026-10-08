"""Plain-assert checks for the overview-based refresh: python3 tests/test_lightpass.py"""
import importlib.util
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.extractors import Extractor  # noqa: E402
from core.filemanager import FileManager  # noqa: E402
from game import lightpass  # noqa: E402
from game.village import Village  # noqa: E402

logging.disable(logging.CRITICAL)
HOUR = 3600
NOW = 1_000_000


def row(vid, name, xy, points, wood, stone, iron, storage, merchants, farm,
        attacked=False, notes=True, classes=("res wood", "res stone", "res iron")):
    """One village row of the production overview, the way the game prints it."""
    def dotted(n):
        text = "%d" % n
        return text if n < 1000 else '%s<span class="grey">.</span>%s' % (text[:-3], text[-3:])
    icon = ('<img src="https://cdn.example/graphic/command/attack.webp" '
            'title="Incoming attack (2)"/>' if attacked else "")
    return (
        '<tr class="nowrap">' + ("<td></td>" if notes else "") +
        '<td><span class="quickedit-vn" data-id="%s" data-length="32">'
        '<span class="quickedit-content"><a href="/game.php?village=%s&amp;screen=overview">%s'
        '<span class="quickedit-label" data-text="%s"> %s (%d|%d) K55 </span></a></span></span></td>'
        % (vid, vid, icon, name, name, xy[0], xy[1]) +
        "<td>%s</td>" % dotted(points) +
        "<td>" + " ".join('<span class="%s">%s</span>' % (cls, dotted(n))
                          for cls, n in zip(classes, (wood, stone, iron))) + " </td>" +
        "<td>%d</td>" % storage +
        '<td><a href="/game.php?village=%s&amp;screen=market">%s</a></td>' % (vid, merchants) +
        '<td class="">%s</td><td> </td><td></td><td></td></tr>' % farm)


PAGE = (
    '<table id="production_table" class="vis overview_table">'
    "<tr><th></th><th>Village (3)</th><th>Points</th><th>Resources</th><th>Warehouse</th>"
    "<th>Merchants</th><th>Farm</th><th>Build</th><th>Research</th><th>Recruit</th></tr>"
    + row("101", "Alpha &amp; co", (500, 501), 8836, 38677, 4279, 51548, 400000, "0/165",
          "10978/24000")
    + row("102", "Beta", (502, 503), 912, 800, 120000, 7, 142373, "12/40", "900/1000",
          attacked=True, classes=("res wood", "warn_90 stone", "res iron"))
    + row("103", "Gamma", (504, 505), 5000, 1000, 2000, 3000, 50000, "3/3", "10/20",
          notes=False)
    + "</table>")


class Reporter:
    def report(self, *a, **k):
        pass


class Wrapper:
    """Records every request, and answers none of them."""

    def __init__(self):
        self.urls = []
        self.reporter = Reporter()

    def get_url(self, url, headers=None):
        self.urls.append(url)

    def get_action(self, **kwargs):
        self.urls.append(str(kwargs))


def world():
    FileManager.set_data_dir(tempfile.mkdtemp(prefix="twb-light-test-"))
    FileManager.create_directories(["cache/managed"])


def snapshot(vid, last_run=NOW - HOUR, **extra):
    data = {"name": "Alpha & co", "public": {"points": 1, "location": [1, 1]},
            "resources": {"wood": 1, "stone": 1, "iron": 1, "pop": 1},
            "available_troops": {"spear": "5"}, "troops": {"spear": 9},
            "buidling_levels": {}, "production": {"wood": 100}, "scavenge_state": None,
            "storage_max": 1, "last_run": last_run}
    data.update(extra)
    FileManager.save_json_file(data, lightpass.SNAPSHOT % vid)
    return data


def config(**villages):
    """An account where nothing works inside a village, like a grown world."""
    return {
        "bot": {}, "world": {}, "market": {}, "balancer": {"enabled": False},
        "building": {"manage_buildings": True},
        "units": {"recruit": True, "upgrade": True},
        "farms": {"farm": False, "scavenge": True,
                  "mass_scavenge": {"enabled": False}},
        "account_manager": {"enabled": True, "building": True, "recruiting": True,
                            "research": True},
        "villages": villages or {"101": {"managed": True, "farm_enabled": False}},
    }


def reason(cfg, snap=True, vid="101", name=None):
    village = Village(wrapper=Wrapper(), village_id=vid)
    village.village_set_name = name
    return village.visit_reason(cfg, snapshot(vid) if snap is True else snap)


def test_production_overview_reads_every_row():
    out = Extractor.production_overview(PAGE)
    assert sorted(out) == ["101", "102", "103"]
    assert out["101"] == {
        "name": "Alpha & co", "location": [500, 501], "points": 8836,
        "resources": {"wood": 38677, "stone": 4279, "iron": 51548},
        "storage_max": 400000, "merchants_free": 0, "merchants_total": 165,
        "pop_used": 10978, "pop_max": 24000, "under_attack": False}
    # A nearly full warehouse changes the span's class, not its meaning.
    assert out["102"]["resources"] == {"wood": 800, "stone": 120000, "iron": 7}
    assert out["102"]["under_attack"] and out["102"]["merchants_free"] == 12
    # The notes column is not always there.
    assert out["103"]["points"] == 5000 and out["103"]["location"] == [504, 505]


def test_a_row_that_cannot_be_read_whole_is_left_out():
    broken = PAGE.replace('screen=market">12/40</a>', 'screen=market">?</a>')
    assert sorted(Extractor.production_overview(broken)) == ["101", "103"]
    assert Extractor.production_overview("<html>logged out</html>") == {}
    assert Extractor.production_overview(None) == {}


def test_refresh_keeps_what_the_overview_does_not_carry():
    world()
    before = snapshot("101")
    data = Extractor.production_overview(PAGE)["101"]
    after = lightpass.refresh_snapshot("101", data, now=NOW)
    assert after["resources"] == {"wood": 38677, "stone": 4279, "iron": 51548,
                                  "pop": 24000 - 10978}
    assert after["storage_max"] == 400000 and after["public"]["points"] == 8836
    assert after["public"]["location"] == [500, 501]
    assert after["last_run"] == NOW
    # Written by a visit before this existed, so that visit is the last one.
    assert after["last_visit"] == NOW - HOUR
    for kept in ("production", "buidling_levels", "scavenge_state",
                 "available_troops", "troops"):
        assert after[kept] == before[kept]
    assert FileManager.load_json_file(lightpass.SNAPSHOT % "101") == after
    assert lightpass.refresh_snapshot("999", data, now=NOW) is None


def test_refresh_takes_troops_from_a_fresh_units_overview_only():
    world()
    snapshot("101")
    data = Extractor.production_overview(PAGE)["101"]
    located = {"101": {"own": {"spear": 400, "axe": 0}, "in_village": {},
                       "elsewhere": {"spear": 100, "axe": 0},
                       "moving": {"spear": 50, "axe": 7}}}
    FileManager.save_json_file(
        {"by_village": located, "when": NOW - 7200, "complete_when": NOW - 7200},
        lightpass.TROOPS)
    assert lightpass.refresh_snapshot("101", data, now=NOW)["troops"] == {"spear": 9}
    FileManager.save_json_file(
        {"by_village": located, "when": NOW - 60, "complete_when": NOW - 60},
        lightpass.TROOPS)
    after = lightpass.refresh_snapshot("101", data, now=NOW)
    assert after["available_troops"] == {"spear": "400"}
    assert after["troops"] == {"spear": 550, "axe": 7}


def test_periodic_visits_are_few_and_oldest_first():
    snaps = {str(i): {"last_visit": NOW - i * HOUR} for i in range(1, 40)}
    assert lightpass.due_for_visit(snaps, 24, 3, NOW) == {"39", "38", "37"}
    assert lightpass.due_for_visit(snaps, 48, 3, NOW) == set()
    assert lightpass.due_for_visit(snaps, 0, 3, NOW) == set()
    # No last_visit yet: the snapshot's own time is when it was last visited.
    assert lightpass.due_for_visit({"7": {"last_run": NOW - 30 * HOUR}}, 24, 3, NOW) == {"7"}


def test_a_village_with_nothing_switched_on_needs_no_visit():
    world()
    assert reason(config()) is None


def test_anything_that_works_inside_the_village_is_a_reason():
    world()
    assert reason(config(), snap=None) == "never visited"
    assert reason(config(**{"101": {"managed": False}})) == "not managed"
    assert reason(config(), name="001 Home") == "to be renamed"
    assert reason(config(), name="Alpha & co") is None

    cfg = config()
    cfg["account_manager"]["building"] = False
    assert reason(cfg) == "building"
    cfg["villages"]["101"]["building"] = False     # the per-village off-switch
    assert reason(cfg) is None

    cfg = config()
    cfg["account_manager"]["recruiting"] = False
    assert reason(cfg) == "recruiting"
    cfg = config()
    cfg["account_manager"]["research"] = False
    assert reason(cfg) == "research"
    cfg = config()
    cfg["account_manager"]["enabled"] = False       # nobody else is doing it
    assert reason(cfg) == "building"

    cfg = config()
    cfg["farms"]["farm"] = True
    assert reason(cfg) is None                       # farm_enabled is off here
    cfg["villages"]["101"]["farm_enabled"] = True
    assert reason(cfg) == "farming"
    cfg = config()
    cfg["farms"]["barb_shaper"] = True
    assert reason(cfg) == "barb shaper"
    cfg = config(**{"101": {"managed": True, "farm_enabled": False, "snobs": 2}})
    assert reason(cfg) == "nobles"
    cfg = config(**{"101": {"managed": True, "farm_enabled": False,
                            "gather_enabled": True}})
    assert reason(cfg) == "scavenging"
    cfg["farms"]["scavenge"] = False                 # the account-wide switch
    assert reason(cfg) is None

    cfg = config()
    cfg["market"]["auto_trade"] = True
    assert reason(cfg) == "market trading"
    cfg = config()
    cfg["units"]["manage_defence"] = True
    assert reason(cfg) == "defence"


def test_a_pending_scavenge_unlock_is_a_reason():
    world()
    cfg = config(**{"101": {"managed": True, "farm_enabled": False,
                            "scavenge_unlock_enabled": True}})
    # No building levels known: the unlock step has nothing to act on either.
    assert reason(cfg) is None
    assert reason(cfg, snap=snapshot("101", buidling_levels={"main": 9})) == "scavenge unlock"
    done = [{"option": o, "locked": False} for o in (1, 2, 3)]
    assert reason(cfg, snap=snapshot("101", buidling_levels={"main": 9},
                                     scavenge_state=done)) is None
    # A village the mass pass scavenges has no scavenge_state; what the unlock
    # check last saw on the screen answers instead. Option 4 needs HQ 15.
    assert reason(cfg, snap=snapshot("101", buidling_levels={"main": 9},
                                     scavenge_locked=[4])) is None
    assert reason(cfg, snap=snapshot("101", buidling_levels={"main": 9},
                                     scavenge_locked=[3, 4])) == "scavenge unlock"
    assert reason(cfg, snap=snapshot("101", buidling_levels={"main": 15},
                                     scavenge_locked=[4])) == "scavenge unlock"


def test_a_light_village_opens_nothing_when_its_merchants_are_out():
    world()
    cfg = config(**{"101": {"managed": True, "farm_enabled": False},
                    "102": {"managed": True, "farm_enabled": False}})
    cfg["balancer"] = {"enabled": True, "sender_min_points": 4000,
                       "receiver_max_points": 1000}
    rows = Extractor.production_overview(PAGE)
    rows["102"]["resources"] = {"wood": 800, "stone": 900, "iron": 7}   # room to fill
    for vid in ("101", "102"):
        snapshot(vid)
        lightpass.refresh_snapshot(vid, rows[vid], now=NOW)
    wrapper = Wrapper()
    village = Village(wrapper=wrapper, village_id="101")
    assert village.run_light(cfg, rows["101"]) == 0        # 0/165 merchants home
    assert wrapper.urls == []
    # With merchants home it goes to the market, as a visit would have.
    rows["101"]["merchants_free"] = 20
    village.run_light(cfg, rows["101"])
    assert len(wrapper.urls) == 1 and "screen=market" in wrapper.urls[0]


# --- the main loop's two hooks, on a bot with no game behind it ----------------

def load_twb():
    here = os.getcwd()
    spec = importlib.util.spec_from_file_location(
        "twb_under_test", os.path.join(os.path.dirname(__file__), "..", "twb.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    os.chdir(here)
    logging.disable(logging.CRITICAL)
    return module


class Page:
    def __init__(self, text):
        self.text = text


class Game(Wrapper):
    """Serves the production overview and records everything asked of it."""
    page = PAGE

    def get_url(self, url, headers=None):
        self.urls.append(url)
        return Page(self.page) if "mode=prod" in url else None


def bot(villages=("101", "102", "103")):
    world()
    twb = load_twb()
    twb.time.time = lambda: bot.now
    lightpass.time.time = lambda: bot.now
    b = twb.TWB.__new__(twb.TWB)
    b.wrapper = Game()
    b.found_villages = list(villages)
    b.villages = [Village(wrapper=b.wrapper, village_id=v) for v in villages]
    b.update_troop_movements = b.update_troop_templates = b.heartbeat = lambda: None
    for vid in villages:
        snapshot(vid, last_run=bot.now - HOUR)
    return b


bot.now = NOW
LIGHT = {"enabled": True, "full_visit_hours": 24, "full_visits_per_pass": 3,
         "cycle_minutes": 60}


def a_pass(b, cfg, light=LIGHT):
    """One cycle's worth of the two hooks. Returns (mode, {village: reason})."""
    mode, production, snapshots, due = b.light_prepare(light)
    return mode, {v.village_id: b.light_handle(v, cfg, mode, production, snapshots, due)
                  for v in b.villages}


def three(**overrides):
    villages = {v: {"managed": True, "farm_enabled": False} for v in ("101", "102", "103")}
    for vid, extra in overrides.items():
        villages["1" + vid[1:]].update(extra)
    return config(**villages)


def test_a_quiet_account_costs_one_request_per_refresh():
    bot.now = NOW
    b = bot()
    mode, reasons = a_pass(b, three())
    assert mode == "refresh" and set(reasons.values()) == {None}
    assert len(b.wrapper.urls) == 1 and "mode=prod" in b.wrapper.urls[0]
    assert "page=-1" in b.wrapper.urls[0]
    assert FileManager.load_json_file(lightpass.SNAPSHOT % "102")["storage_max"] == 142373

    # Inside the refresh interval the villages are passed over, for nothing.
    bot.now = NOW + 20 * 60
    mode, reasons = a_pass(b, three())
    assert mode == "skip" and set(reasons.values()) == {None}
    assert len(b.wrapper.urls) == 1

    bot.now = NOW + 61 * 60
    assert a_pass(b, three())[0] == "refresh" and len(b.wrapper.urls) == 2


def test_a_village_with_work_keeps_its_visit_every_cycle():
    bot.now = NOW
    b = bot()
    cfg = three(v02={"farm_enabled": True})
    cfg["farms"]["farm"] = True
    assert a_pass(b, cfg)[1] == {"101": None, "102": "farming", "103": None}
    bot.now = NOW + 5 * 60
    assert a_pass(b, cfg) == ("skip", {"101": None, "102": "farming", "103": None})
    assert "farming" not in lightpass.NOT_WORK


def test_an_unreadable_overview_means_every_village_is_visited():
    bot.now = NOW
    b = bot()
    b.wrapper.page = "<html>something else</html>"
    mode, reasons = a_pass(b, three())
    assert mode is None and set(reasons.values()) == {"light villages off"}
    assert b.light_refreshed_at == 0          # and it is tried again next cycle
    b.wrapper.page = PAGE
    assert a_pass(b, three())[0] == "refresh"


def test_a_village_missing_from_the_overview_is_visited():
    bot.now = NOW
    b = bot(villages=("101", "102", "103", "104"))
    cfg = three()
    cfg["villages"]["104"] = {"managed": True, "farm_enabled": False}
    assert a_pass(b, cfg)[1]["104"] == "not on the overview"


def test_off_by_default_and_then_nothing_changes():
    bot.now = NOW
    b = bot()
    off = lightpass.settings({"bot": {}})
    assert off["enabled"] is False
    mode, reasons = a_pass(b, three(), light=off)
    assert mode is None and set(reasons.values()) == {"light villages off"}
    assert b.wrapper.urls == []


def test_periodic_visits_come_round_a_few_at_a_time():
    bot.now = NOW
    b = bot()
    for vid, age in (("101", 30), ("102", 50), ("103", 2)):
        snapshot(vid, last_run=NOW - age * HOUR)
    one = dict(LIGHT, full_visits_per_pass=1)
    assert a_pass(b, three(), light=one)[1] == {"101": None, "102": "periodic visit", "103": None}
    assert "periodic visit" in lightpass.NOT_WORK


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
