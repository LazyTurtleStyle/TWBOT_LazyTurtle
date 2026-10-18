"""Plain-assert checks for reading incoming convoys off the transport overview:
python3 tests/test_balancer_transport.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game import balancer as balancer_module  # noqa: E402
from game.balancer import ResourceBalancer, parse_transport_overview  # noqa: E402


def amount(n):
    """How the overview writes a number: the thousands dot in its own span."""
    if n < 1000:
        return str(n)
    return '%d<span class="grey">.</span>%03d' % (n // 1000, n % 1000)


def row(source, target, wood, stone, iron):
    cells = "".join(
        '<span class="nowrap"><span class="icon header %s" title="x"> </span>%s</span> '
        % (res, amount(n)) for res, n in (("wood", wood), ("stone", stone), ("iron", iron)))
    return ('<tr class="row_a"><td><input type="checkbox" /></td><td></td>'
            '<td><a href="/game.php?village=1&amp;screen=info_player&amp;id=9">Me</a></td>'
            '<td><a href="/game.php?village=1&amp;screen=info_village&amp;id=%s">A</a></td>'
            '<td><a href="/game.php?village=1&amp;screen=info_village&amp;id=%s">B</a></td>'
            '<td>today</td><td><span class="timer">0:01:55</span></td><td>35</td>'
            '<td>%s</td></tr>' % (source, target, cells))


def page(*rows):
    return ('<a href="game.php?screen=overview_villages&amp;mode=trader">x</a>'
            '<table id="trades_table" class="vis"><tr><th>head</th></tr>%s</table>'
            % "".join(rows))


def test_convoys_are_summed_per_receiving_village():
    got = parse_transport_overview(page(row("1", "2", 16354, 7233, 11413),
                                        row("3", "2", 500, 0, 1000),
                                        row("1", "4", 0, 9886, 0)))
    assert got == {"2": {"wood": 16854, "stone": 7233, "iron": 12413},
                   "4": {"wood": 0, "stone": 9886, "iron": 0}}


def test_no_convoys_is_an_empty_answer_not_a_failure():
    text = '<a href="game.php?screen=overview_villages&amp;mode=trader">x</a>'
    assert parse_transport_overview(text) == {}
    assert parse_transport_overview("<html>logged out</html>") is None


class Page:
    def __init__(self, text):
        self.text = text


class Wrapper:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    def get_url(self, url):
        self.reads += 1
        return Page(self.text)


def balancer(text):
    b = ResourceBalancer(Wrapper(text), "1")
    b.use_transport_overview = True
    balancer_module._TRANSPORT.update(ts=0, incoming=None)
    return b


def test_receiver_state_adds_production_since_the_snapshot_and_own_later_sends():
    b = balancer(page(row("9", "2", 1000, 0, 0)))
    now = time.time()
    village = {"resources": {"wood": 10000, "stone": 10000, "iron": 10000},
               "storage_max": 50000, "last_run": now - 1800,
               "production": {"wood": 2000, "stone": 0, "iron": 0}}
    balancer_module._load_state = lambda: {"_inflight": {"2": [
        {"sent": now + 1, "arrival": now + 600, "amounts": {"iron": 3000}},
        {"sent": now - 99999, "arrival": now + 600, "amounts": {"iron": 7000}},
    ]}}
    b._transport_incoming()
    # A send the bot made after the overview was read is not on it yet.
    balancer_module._TRANSPORT["ts"] = now
    state = b.receiver_state("2", village)
    assert state["resources"]["wood"] == 11000      # half an hour of 2000/h
    assert state["incoming"] == {"wood": 1000, "stone": 0, "iron": 3000}
    assert state["storage_max"] == 50000


def test_the_overview_is_read_once_for_many_receivers():
    b = balancer(page(row("9", "2", 1000, 0, 0)))
    balancer_module._load_state = lambda: {}
    village = {"resources": {"wood": 1, "stone": 1, "iron": 1}, "storage_max": 10,
               "last_run": time.time()}
    for vid in ("2", "3", "4"):
        b.receiver_state(vid, village)
    assert b.wrapper.reads == 1


def test_it_falls_back_to_the_market_page_when_the_overview_fails():
    b = balancer("<html>logged out</html>")
    balancer_module._load_state = lambda: {}
    b.read_receiver = lambda vid: "market page"
    village = {"resources": {"wood": 1, "stone": 1, "iron": 1}, "storage_max": 10,
               "last_run": time.time()}
    assert b.receiver_state("2", village) == "market page"


def test_a_convoy_that_landed_after_the_snapshot_counts_as_stock():
    b = balancer(page())
    now = time.time()
    village = {"resources": {"wood": 10000, "stone": 10000, "iron": 10000},
               "storage_max": 76000, "last_run": now - 3600}
    balancer_module._load_state = lambda: {"_inflight": {"2": [
        {"sent": now - 5000, "arrival": now - 600, "amounts": {"stone": 9000}},
        {"sent": now - 9000, "arrival": now - 7200, "amounts": {"iron": 5000}},
    ]}}
    state = b.receiver_state("2", village)
    # Landed ten minutes ago, after the snapshot: in the stock now.
    assert state["resources"]["stone"] == 19000
    # Landed before the snapshot: the snapshot already has it.
    assert state["resources"]["iron"] == 10000
    assert state["incoming"] == {"wood": 0, "stone": 0, "iron": 0}


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
