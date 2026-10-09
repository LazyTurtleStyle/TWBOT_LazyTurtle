"""Plain-assert checks for the village-group refresh: python3 tests/test_group_refresh.py"""
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.filemanager import FileManager  # noqa: E402
from game import incomings  # noqa: E402

logging.disable(logging.CRITICAL)
HOUR = 3600
MENU = [
    {"group_id": "0", "name": "all", "type": "group_all"},
    {"group_id": "11", "name": "OFF", "type": "group_static"},
    {"group_id": "12", "name": "DEF &amp; co", "type": "group_static"},
    {"group_id": "21", "name": "Scavengers", "type": "group_dynamic"},
    {"group_id": "22", "name": "Under attack", "type": "group_dynamic"},
    {"group_id": "23", "name": "West", "type": "group_dynamic"},
]
# village -> the manual groups it is in, as the manual-groups overview prints them
ASSIGNED = {"101": ["OFF"], "102": ["OFF", "DEF &amp; co"], "103": []}
DYNAMIC = {"21": ["101", "102", "103"], "22": ["102"], "23": ["101"]}


def manual_page(assigned=ASSIGNED, counts=None):
    rows = "".join(
        '<tr><td><span class="quickedit-vn" data-id="%s"></span></td>'
        '<td id="assigned_groups_%s_count">%d</td><td id="assigned_groups_%s_points">1</td>'
        '<td id="assigned_groups_%s_names">%s</td></tr>'
        % (vid, vid, (counts or {}).get(vid, len(names)), vid, vid, "; ".join(names))
        for vid, names in assigned.items())
    return '<table id="group_assign_table">%s</table>' % rows


class Page:
    def __init__(self, text):
        self.text = text


class Game:
    """Answers the three kinds of request the refresh makes, and counts them."""

    def __init__(self):
        self.urls = []
        self.manual = manual_page()
        self.menu = MENU

    def get_url(self, url, headers=None):
        self.urls.append(url)
        if "load_group_menu" in url:
            menu = [dict(g, name=g["name"].replace("&amp;", "&")) for g in self.menu]
            return Page(json.dumps({"result": menu}))
        if "mode=groups&type=static" in url:
            return Page(self.manual)
        for gid, members in DYNAMIC.items():
            if "group=%s&" % gid in url:
                return Page("".join(
                    '<span class="quickedit-vn" data-id="%s">' % v for v in members))
        for gid in ("11", "12"):                       # a manual group, the old way
            if "group=%s&" % gid in url:
                name = [g["name"] for g in MENU if g["group_id"] == gid][0]
                return Page("".join(
                    '<span class="quickedit-vn" data-id="%s">' % v
                    for v, names in ASSIGNED.items() if name in names))
        return Page("")

    def kinds(self):
        return ["menu" if "load_group_menu" in u else
                "manual page" if "type=static" in u else
                "group " + u.split("group=")[1].split("&")[0] for u in self.urls]


def refresh(game, at, **kwargs):
    incomings.time.time = lambda: at
    game.urls.clear()
    incomings.IncomingManager(village_id="101", wrapper=game, **kwargs).ensure_groups()
    return {g["name"]: g["villages"] for g in incomings.load_groups()}


def world():
    FileManager.set_data_dir(tempfile.mkdtemp(prefix="twb-groups-test-"))
    FileManager.create_directories(["cache/world"])
    return Game()


EVERYONE = {"OFF": ["101", "102"], "DEF & co": ["102"],
            "Scavengers": ["101", "102", "103"], "Under attack": ["102"], "West": ["101"]}


def test_manual_groups_come_from_one_page():
    game = world()
    assert refresh(game, 1_000_000) == EVERYONE
    # Two manual groups cost one request; each dynamic one still costs its own.
    assert game.kinds() == ["menu", "manual page", "group 21", "group 22", "group 23"]


def test_nothing_is_asked_inside_the_hour():
    game = world()
    refresh(game, 1_000_000)
    assert refresh(game, 1_000_000 + 1800) == EVERYONE and game.urls == []


def test_by_default_every_dynamic_group_is_read_each_time():
    game = world()
    refresh(game, 1_000_000)
    refresh(game, 1_000_000 + HOUR)
    assert game.kinds().count("manual page") == 1
    assert [k for k in game.kinds() if k.startswith("group")] == ["group 21", "group 22", "group 23"]
    refresh(game, 1_000_000 + 2 * HOUR, dynamic_group_hours=1)
    assert len(game.urls) == 5


def test_only_the_groups_the_bot_uses_are_read_every_hour():
    game = world()
    settings = dict(hot_groups={"scavengers", "22"}, dynamic_group_hours=6)
    refresh(game, 1_000_000, **settings)
    assert len(game.urls) == 5                         # nothing cached yet
    DYNAMIC["23"] = ["101", "103"]                      # West changes in game
    try:
        got = refresh(game, 1_000_000 + HOUR, **settings)
        # Scavengers by name, Under attack by id; West is left standing.
        assert game.kinds() == ["menu", "manual page", "group 21", "group 22"]
        assert got["West"] == ["101"]
        got = refresh(game, 1_000_000 + 5 * HOUR, **settings)
        assert "group 23" not in game.kinds()
        got = refresh(game, 1_000_000 + 6 * HOUR, **settings)
        assert "group 23" in game.kinds() and got["West"] == ["101", "103"]
        # ... and its six hours start again from there.
        refresh(game, 1_000_000 + 7 * HOUR, **settings)
        assert "group 23" not in game.kinds()
    finally:
        DYNAMIC["23"] = ["101"]


def test_a_new_dynamic_group_is_read_straight_away():
    game = world()
    settings = dict(hot_groups=set(), dynamic_group_hours=12)
    game.menu = MENU[:-1]
    refresh(game, 1_000_000, **settings)
    game.menu = MENU
    got = refresh(game, 1_000_000 + HOUR, **settings)
    assert game.kinds() == ["menu", "manual page", "group 23"] and got["West"] == ["101"]


def test_a_page_that_cannot_be_trusted_falls_back_to_one_read_per_group():
    for broken in (
            "<html>something else</html>",
            manual_page(counts={"102": 3}),                       # a name with the separator in it
            manual_page(dict(ASSIGNED, **{"103": ["Unknown"]}))):  # not a group we know
        game = world()
        game.manual = broken
        assert refresh(game, 1_000_000) == EVERYONE
        assert game.kinds() == ["menu", "manual page", "group 11", "group 12",
                                "group 21", "group 22", "group 23"]


def test_two_manual_groups_with_one_name_are_read_the_old_way():
    game = world()
    game.menu = MENU + [{"group_id": "13", "name": "OFF", "type": "group_static"}]
    refresh(game, 1_000_000)
    assert "manual page" not in game.kinds() and "group 13" in game.kinds()


def test_an_account_without_manual_groups_asks_for_no_page():
    game = world()
    game.menu = [g for g in MENU if g["type"] != "group_static"]
    refresh(game, 1_000_000)
    assert game.kinds() == ["menu", "group 21", "group 22", "group 23"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
