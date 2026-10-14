"""Plain-assert checks for the sat-account overview: python3 tests/test_sitter.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.extractors import Extractor  # noqa: E402
from game import sitter  # noqa: E402

NOW = 2_000_000_000
SETTINGS = (
    '<h3>Eigen vakantievervangingen</h3><table class="vis"><tr><th>Speler</th></tr><tr><td>'
    '<a href="/game.php?village=1&amp;screen=info_player&amp;id=4242" title="Tribe">Friend &amp; co</a> '
    '<span class="icon info-med sitter_details" data-sat-player="4242"></span></td><td>'
    '<a href="/game.php?village=1&amp;screen=settings&amp;action=sitter_login&amp;player=4242&amp;h=x" class="btn">Inloggen</a>'
    '</td></tr></table>')


def game_page(player_id, body=""):
    data = {"player": {"id": str(player_id), "name": "Friend", "points": "1234", "rank": 7},
            "village": {"id": 1}, "time_generated": NOW * 1000}
    return "<script>TribalWars.updateGameData(%s);</script>%s" % (json.dumps(data), body)


PROD = ('<table id="production_table"><tr><th>Village</th></tr><tr><td>'
        '<span class="quickedit-vn" data-id="11"><span class="quickedit-label" data-text="North">North (500|500) K55</span></span></td>'
        '<td>5<span class="grey">.</span>000</td><td><span class="res wood">100</span> <span class="res stone">200</span> '
        '<span class="res iron">300</span></td><td>40000</td><td><a href="x&amp;screen=market">10/20</a></td><td>900/24000</td></tr></table>')


class Page:
    def __init__(self, text):
        self.text = text


class Account:
    """Serves the three overview pages as the given player."""

    def __init__(self, player_id, captcha=False, logged_out=False):
        self.player_id, self.captcha, self.logged_out, self.urls = player_id, captcha, logged_out, []

    def get_url(self, url):
        self.urls.append(url)
        if self.captcha:
            return Page('<div data-bot-protect="forced"></div>')
        if self.logged_out:
            return Page("<html>login</html>")
        return Page(game_page(self.player_id, PROD if "mode=prod" in url else ""))


TROOPS = {"11": {"own": {"spear": 1000, "axe": 2000, "snob": 1, "heavy": 10},
                 "in_village": {"spear": 3000, "heavy": 10}, "elsewhere": {"spear": 500},
                 "moving": {"axe": 100}}}
INCOMING = [{"command_id": "c1", "target_id": "11", "game_label": "Edel", "arrival_ms": (NOW + 600) * 1000,
             "target_coords": [500, 500], "origin_coords": [510, 510]},
            {"command_id": "c2", "target_id": "11", "game_label": "Ram", "arrival_ms": (NOW + 60) * 1000},
            {"command_id": "c3", "target_id": "11", "game_label": "Ram", "arrival_ms": (NOW - 60) * 1000}]


def read(account, troops=TROOPS):
    original = Extractor.units_overview_complete
    Extractor.units_overview_complete = staticmethod(lambda res: troops)
    try:
        return sitter.read_overview(account, "4242", lambda html, now: list(INCOMING), now=NOW)
    finally:
        Extractor.units_overview_complete = original


def test_the_accounts_you_sit_for_are_read_off_the_settings_page():
    assert sitter.sat_accounts(SETTINGS) == [{"player_id": "4242", "name": "Friend & co"}]
    assert sitter.sat_accounts("<html>no replacements</html>") == []


def test_three_pages_of_the_sat_account_make_the_overview():
    account = Account("4242")
    state, error = read(account)
    assert error is None and len(account.urls) == 3
    assert all("t=4242" in u and "page=-1" in u for u in account.urls)
    v = state["villages"][0]
    assert (v["name"], v["location"], v["points"]) == ("North", [500, 500], 5000)
    assert v["off_home"] == 2000 and v["def_home"] == 1000 + 60
    assert v["off_total"] == 2100 and v["def_total"] == 1500 + 60 and v["nobles"] == 1
    assert v["def_standing"] == 3060
    # Only what is still on its way counts; the one that landed does not.
    assert (v["attacks"], v["nobles_incoming"], v["first_hit_ms"]) == (2, 1, (NOW + 60) * 1000)
    assert [c["command_id"] for c in state["incomings"]] == ["c2", "c1"]
    assert state["totals"]["attacks"] == 2 and state["player"]["id"] == "4242"


def test_pages_of_any_other_account_are_never_shown_as_this_one():
    # A session that ran out, or the player's own session, answers with
    # their own pages: those must not pass for the sat account's.
    assert read(Account("1111")) == (None, "not this account's pages (log in as replacement again)")
    assert read(Account("4242", logged_out=True)) == (None, "logged out")
    assert read(Account("4242", captcha=True)) == (None, "captcha")


def test_the_dashboard_never_hands_the_session_back():
    import tempfile
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webmanager"))
    from webmanager.utils import DataReader
    root = tempfile.mkdtemp(prefix="twb-sitter-test-")
    DataReader.data_path = staticmethod(lambda *parts: os.path.join(root, *parts))
    DataReader.ensure_data_dir = staticmethod(lambda *parts: os.makedirs(os.path.join(root, *parts), exist_ok=True))
    assert DataReader.sitter_session_set("") is False
    assert DataReader.sitter_session_set("sid=SECRET123; cid=42", "4242x") is True
    stored = json.load(open(os.path.join(root, "cache", "sitter_session.json")))
    assert stored["cookies"]["sid"] == "SECRET123" and stored["player_id"] == "4242"
    view = DataReader.sitter_view()
    assert view["has_session"] and "SECRET123" not in json.dumps(view)
    assert DataReader.sitter_refresh() and DataReader.sitter_view()["pending"]
    DataReader.sitter_session_clear()
    assert not DataReader.sitter_view()["has_session"] and not DataReader.sitter_view()["pending"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
