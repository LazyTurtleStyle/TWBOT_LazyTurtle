"""Plain-assert checks for the poller's captcha alert: python3 tests/test_incoming_captcha.py"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.filemanager import FileManager  # noqa: E402
from core.request import WebWrapper  # noqa: E402
from game import incomings  # noqa: E402

logging.disable(logging.CRITICAL)
HOUR = 3600
BLOCKED = '<body data-bot-protect="forced"></body>'
# Enough for Extractor.game_state to call it a logged-in page with no incomings.
FINE = '<script>TribalWars.updateGameData({"player":{"id":1},"village":{"id":1}});</script>'


class Page:
    def __init__(self, text):
        self.text = text


class Wrapper:
    """Hands back whatever page the test says the game is serving."""
    CAPTCHA_BLOCK_FILE = WebWrapper.CAPTCHA_BLOCK_FILE
    text = FINE

    def get_url(self, url, headers=None):
        return Page(self.text)


class World:
    """A throwaway data dir, a clock the test moves, and the messages sent."""

    def __init__(self, active=(5 * HOUR, 23 * HOUR + 1800)):
        self.dir = tempfile.mkdtemp(prefix="twb-captcha-test-")
        FileManager.set_data_dir(self.dir)
        FileManager.create_directories(["cache/world", incomings.INCOMINGS_DIR])
        self.now = 0
        self.sent = []
        self.wrapper = Wrapper()
        self.active = active
        incomings.time.time = lambda: self.now
        incomings.Notification.send = lambda message, category=None: self.sent.append(
            (self.clock(), message))

    def clock(self):
        return "%02d:%02d" % ((self.now // HOUR) % 24, (self.now // 60) % 60)

    def night(self):
        return not self.active[0] <= self.now % (24 * HOUR) <= self.active[1]

    def poll(self, at, blocked):
        """One poller pass at `at` (seconds since midnight of day one)."""
        self.now = at
        self.wrapper.text = BLOCKED if blocked else FINE
        manager = incomings.IncomingManager(
            village_id="1", wrapper=self.wrapper, captcha_alerts=True,
            captcha_reminder_hours=3, night_check=self.night)
        manager._capture_label_endpoint = lambda res, now: None
        return manager.update_incomings()

    def run(self, start, end, blocked, step=480):
        for at in range(int(start), int(end), step):
            self.poll(at, blocked)

    def main_loop_hits(self, at):
        """The main loop ran into the captcha itself and announced it."""
        self.sent.append(("%02d:%02d" % ((at // HOUR) % 24, (at // 60) % 60), "main: hit"))
        FileManager.save_json_file({"since": int(at)}, WebWrapper.CAPTCHA_BLOCK_FILE)

    def main_loop_clears(self):
        FileManager.remove_file(WebWrapper.CAPTCHA_BLOCK_FILE)

    def times(self, needle):
        return [at for at, message in self.sent if needle in message]


def at(clock, day=0):
    hours, minutes = clock.split(":")
    return day * 24 * HOUR + int(hours) * HOUR + int(minutes) * 60


def test_a_good_poll_sends_nothing():
    w = World()
    w.run(at("10:00"), at("14:00"), blocked=False)
    assert w.sent == []


def test_one_blocked_read_is_not_an_alert():
    w = World()
    w.poll(at("20:49"), blocked=True)
    w.poll(at("20:57"), blocked=False)
    assert w.sent == []


def test_night_captcha_is_raised_once_and_not_repeated_until_morning():
    # The live night this was written for: first refused at 00:48, the main
    # loop asleep until 05:00, the captcha solved at 07:13.
    w = World()
    w.run(at("00:48"), at("05:00"), blocked=True)
    assert w.times("blocking the incoming-attack check") == ["00:56"]
    assert len(w.sent) == 1
    w.main_loop_hits(at("05:00"))
    w.run(at("05:00"), at("07:13"), blocked=True)
    # The main loop's own message at 05:00 is the morning notice; no echo.
    assert [m for _, m in w.sent] == [w.sent[0][1], "main: hit"]
    w.main_loop_clears()
    w.run(at("07:13"), at("08:00"), blocked=False)
    # ... and it says "captcha cleared" itself, so the poller stays quiet.
    assert len(w.sent) == 2
    assert not FileManager.path_exists(incomings.CAPTCHA_STATE_CACHE)


def test_day_captcha_reminds_every_three_hours():
    # Hit by the main loop at 14:07 and left standing until the evening.
    w = World()
    w.main_loop_hits(at("14:07"))
    w.run(at("14:11"), at("23:59"), blocked=True)
    assert w.times("blocking the incoming-attack check") == []
    # Polls land every 8 minutes, so each reminder is the first poll that is
    # three hours past the last time the user was told.
    assert w.times("still unsolved") == ["17:07", "20:11", "23:15"]
    assert "after 3h 00m" in w.sent[1][1] and "after 9h 08m" in w.sent[3][1]


def test_reminders_stop_for_the_night_and_resume_after_it():
    w = World()
    w.main_loop_hits(at("21:00"))
    w.run(at("21:02"), at("11:00", day=1), blocked=True)
    # 00:00 and 03:00 fall in the night; the next one is the first poll of the
    # morning, then every three hours again.
    assert w.times("still unsolved") == ["05:02", "08:06"]
    assert "after 8h 02m" in w.sent[1][1]


def test_poller_confirms_the_clear_when_only_it_knew():
    w = World()
    w.run(at("01:00"), at("01:30"), blocked=True)
    w.poll(at("01:32"), blocked=False)
    assert [m.split(":")[1].strip()[:15] for _, m in w.sent] == [
        "a captcha is bl", "captcha cleared"]


def test_reminders_can_be_turned_off():
    w = World()
    w.now = at("09:00")
    manager = incomings.IncomingManager(
        village_id="1", wrapper=w.wrapper, captcha_alerts=True,
        captcha_reminder_hours=0, night_check=w.night)
    assert manager._captcha_reminder_due(24 * HOUR) is False


def test_a_leftover_record_does_not_date_the_next_captcha():
    w = World()
    w.run(at("09:00"), at("09:30"), blocked=True)       # bot stopped mid-captcha
    w.sent.clear()
    w.run(at("15:00", day=2), at("15:20", day=2), blocked=True)
    assert w.times("blocking the incoming-attack check") == ["15:08"]
    assert w.times("still unsolved") == []


def test_other_callers_stay_silent():
    w = World()
    w.wrapper.text = BLOCKED
    for now in (at("02:00"), at("02:10"), at("08:00")):
        w.now = now
        incomings.IncomingManager(village_id="1", wrapper=w.wrapper).update_incomings()
    assert w.sent == [] and not FileManager.path_exists(incomings.CAPTCHA_STATE_CACHE)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
