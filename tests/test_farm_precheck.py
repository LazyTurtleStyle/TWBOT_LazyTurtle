"""Plain-assert checks for the farm pass's troop pre-check: python3 tests/test_farm_precheck.py"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.extractors import Extractor  # noqa: E402
from core.filemanager import FileManager  # noqa: E402
from game.attack import AttackManager  # noqa: E402

logging.disable(logging.CRITICAL)
UNITS = ("spear", "sword", "axe", "spy", "light", "heavy")


def page(home, templates=((10, {"spy": 1}), (11, {"spy": 1, "light": 5}))):
    """The parts of the Farm Assistant page the pre-check reads."""
    editor = ""
    for tid, units in templates:
        editor += '<input type="hidden" name="template[%d][id]" value="%d">' % (tid, tid)
        editor += "".join('<input type="text" name="%s[%d]" value="%d">'
                          % (u, tid, units.get(u, 0)) for u in UNITS)
    current = ",".join('"%s":"%d"' % (u, home.get(u, 0)) for u in UNITS)
    return ('<form action="/game.php?screen=am_farm&action=edit_all">%s</form>'
            "<script>Accountmanager.farm.current_units = {%s};</script>" % (editor, current))


class Page:
    def __init__(self, text):
        self.text = text


class Reporter:
    def report(self, *a, **k):
        pass


class Game:
    """Serves the page, and accepts a template send only while troops last."""

    def __init__(self, home, text=None):
        self.home = dict(home)
        self.text = text
        self.sends = []
        self.reporter = Reporter()

    def get_url(self, url, headers=None):
        return Page(self.text if self.text is not None else page(self.home))

    def get_api_action(self, village_id=None, action=None, params=None, data=None):
        self.sends.append(data.get("template_id"))
        needs = {10: {"spy": 1}, 11: {"spy": 1, "light": 5}}[data["template_id"]]
        if any(self.home.get(u, 0) < n for u, n in needs.items()):
            return {"error": "Niet genoeg eenheden beschikbaar"}
        for u, n in needs.items():
            self.home[u] -= n
        return {"success": True}


def manager(home, text=None):
    FileManager.set_data_dir(tempfile.mkdtemp(prefix="twb-farm-test-"))
    FileManager.create_directories(["cache/attacks"])
    game = Game(home, text)
    am = AttackManager(wrapper=game, village_id="1")
    am.logger = logging.getLogger("Attacks")
    am.template_id_scout, am.template_id_minimal = 10, 11
    am.farm_icons = am.fetch_farm_icons()
    am._kind_refusals, am._exhausted_kinds = {}, set()
    return am, game


def test_the_page_is_read_for_templates_and_troops():
    text = page({"spy": 3, "light": 12})
    assert Extractor.farm_assistant_templates(text) == {10: {"spy": 1}, 11: {"spy": 1, "light": 5}}
    assert Extractor.farm_assistant_units(text)["light"] == 12
    assert Extractor.farm_assistant_templates("<html></html>") == {}
    assert Extractor.farm_assistant_units("<html></html>") is None


def test_a_village_with_no_troops_sends_nothing():
    am, game = manager({})
    for target in range(100, 120):
        am._send(str(target), kind="scout")
        am._send(str(target), kind="minimal")
    assert game.sends == []
    assert am._exhausted_kinds == {"scout", "minimal"}


def test_sends_stop_exactly_when_the_troops_run_out():
    am, game = manager({"spy": 3, "light": 20})
    for target in range(100, 110):
        am._send(str(target), kind="scout")
    # Three scouts, three sends, and no refused one after them.
    assert game.sends == [10, 10, 10] and "scout" in am._exhausted_kinds
    am, game = manager({"spy": 9, "light": 12})
    for target in range(100, 110):
        am._send(str(target), kind="minimal")
    assert game.sends == [11, 11]             # twelve light cavalry is two sends of five


def test_one_kind_running_out_does_not_stop_the_other():
    am, game = manager({"spy": 2, "light": 0})
    am._send("100", kind="minimal")
    am._send("101", kind="scout")
    assert game.sends == [10] and am._exhausted_kinds == {"minimal"}


def test_without_the_page_it_tries_as_before():
    am, game = manager({}, text="<html>no editor here</html>")
    for target in range(100, 110):
        am._send(str(target), kind="scout")
    # Nothing to go on, so the old rule applies: give up after three refusals.
    assert game.sends == [10, 10, 10] and "scout" in am._exhausted_kinds


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
