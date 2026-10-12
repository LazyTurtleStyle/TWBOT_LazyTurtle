"""Plain-assert checks for reading farms.template_minimal_troops: python3 tests/test_template_troops.py"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game.attack import parse_template_troops  # noqa: E402
from game.village import Village  # noqa: E402

logging.disable(logging.CRITICAL)


def test_reads_dict_json_and_python_text():
    want = {"light": 2, "spy": 1}
    assert parse_template_troops(want) == want
    assert parse_template_troops('{"light": 2, "spy": 1}') == want
    # what the dashboard used to show, and what crashed the bot once saved
    assert parse_template_troops("{'light': 2, 'spy': 1}") == want
    assert parse_template_troops("") == {}
    assert parse_template_troops(None) == {}


def test_rejects_anything_else():
    for bad in ("light 5", "5", "[1, 2]", '{"light": "5"}', '{"light": -1}',
                '{"light": true}', '{"light": 1.5}', "{light: 5}"):
        try:
            parse_template_troops(bad)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % bad)


def test_village_ignores_an_unreadable_setting():
    village = Village.__new__(Village)
    village.logger = logging.getLogger("test")
    for raw, want in (("light 5", {}), ("{'light': 2}", {"light": 2}), ({"spy": 1}, {"spy": 1})):
        village.get_config = lambda section, parameter, default=None, raw=raw: raw
        assert village.minimal_template_troops() == want


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
