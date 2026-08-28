"""
TWB - an open source Tribal Wars bot
"""
#
# This file is part of the TWB distribution (https://github.com/stefan2200/TWB).
# Copyright (c) 2024 Stefan2200
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import collections
import copy
import datetime
import difflib
import json
import logging
import os
import random
import sys
import signal
import threading
import time
import traceback
import coloredlogs
import requests

from core.notification import Notification
from core.updater import check_update
from core.filemanager import FileManager
from core.instance_lock import InstanceLock
from core.request import WebWrapper
from game.village import Village
from game.incomings import IncomingManager
from game.reports import ReportManager
from game import attack_scheduler
from game import csnipe
from game import dailybonus
from game import snipe
from game.noblebarb import NobleBarbManager, escort_reservations
from game.playerfarm import PlayerFarmManager
from manager import VillageManager
from pages.overview import OverviewPage
from core.exceptions import UnsupportedPythonVersion
from core.extractors import Extractor
from core.server_clock import ServerClock

coloredlogs.install(
    level=logging.DEBUG if "-q" not in sys.argv else logging.INFO,
    fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def setup_file_logging():
    """Persist the bot's output (and any crash) to a rotating log file in the
    active world's cache dir, so it survives the tmux pane / a shutdown.

    Must run after resolve_world_dir() so the path lands in worlds/<name>/cache/.
    """
    from logging.handlers import RotatingFileHandler
    try:
        # Idempotent: called again after adopting a world so the log follows the
        # new data dir instead of writing to two files at once.
        root_logger = logging.getLogger()
        for old in list(root_logger.handlers):
            if isinstance(old, RotatingFileHandler):
                root_logger.removeHandler(old)
                old.close()
        log_path = FileManager._resolve("cache/twb.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)

        # Uncaught exceptions go to stderr by default (lost with the pane); also
        # record them in the log so a crash always leaves a traceback on disk.
        def _log_uncaught(exc_type, exc, tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc, tb)
                return
            logging.getLogger("twb").critical(
                "Uncaught exception - bot crashed", exc_info=(exc_type, exc, tb))
        sys.excepthook = _log_uncaught

        logging.getLogger("twb").info("Logging to %s (rotates at 2MB, keeps 3)", log_path)
    except Exception as exc:  # never let logging setup stop the bot
        logging.warning("Could not set up file logging: %s", exc)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

os.chdir(os.path.dirname(os.path.realpath(__file__)))


# Line-buffer stdout so progress reaches the log as it is printed. A bot started
# from the dashboard has its stdout redirected to a file, where the default block
# buffering can sit on "waiting for ..." messages for minutes - long enough to
# look hung while it is patiently waiting for something the user has to do.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# The --world name this process was started with (None = default/root world).
# Set by resolve_world_dir() before anything reads config or cache.
ACTIVE_WORLD = None


def signal_handler(sig, frame):
    print('Exiting...')
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


class TWB:
    """
    Core class that manages activating times, sleeps, general web wrapper
    Also verifies, merges and updates the config file automatically
    """
    res = None
    wrapper = None
    should_run = True
    runs = 0
    # When this process started. Stamped into the heartbeat so the dashboard can
    # tell "reports stopped coming in" apart from "the bot only just came back
    # up and nothing has had time to return yet" (see OverviewBuilder._farm_stall_state).
    started = int(time.time())
    # Set by get_overview when the overview comes back as a login page; the run
    # loop skips the cycle instead of treating every village as unavailable.
    session_logged_out = False
    # One-shot: compare the host clock to the game server's time once per startup
    # and warn if they diverge (forced-peace windows + scheduled attacks are timed
    # against the host clock, so a skew silently offsets them).
    _clock_checked = False
    # Warn when the host clock differs from server time by more than this (seconds).
    CLOCK_SKEW_WARN_SECONDS = 300
    # A wall-clock gap is a timezone difference, so it is whole minutes wide and
    # invisible to the epoch check above; warn once past a minute.
    _wall_clock_warned = False
    CLOCK_WALL_WARN_SECONDS = 60
    # True after an overview came back logged-in but with zero parseable villages
    # (a parse failure, not an empty account). Used to notify once per transition.
    _village_parse_failed = False
    # Troop-movement data is dashboard-only and doesn't need per-cycle freshness.
    # Refresh it at most this often to avoid two extra full-page GETs every cycle
    # (cuts request volume and the bot-like request count).
    TROOP_MOVE_REFRESH_SECONDS = 300
    # Rally-point troop templates belong to the player, not the village, and
    # only change when the player edits one - but when they do, they want it in
    # the dashboard's picker now, not next cycle. Cheap enough to re-read hourly
    # (and the dashboard can expire this to pull an edit in straight away).
    TEMPLATE_REFRESH_SECONDS = 3600

    def __init__(self):
        # Mutable state must live on the instance: main() retries a crash with
        # a fresh TWB(), and class-level lists survive into it - the retry then
        # runs every village twice and the auto-renamer numbers the duplicates
        # (villages named 004+ instead of 001+).
        self.villages = []
        self.found_villages = []
        # {village_id: {unit: count}} held back for the armed noble jobs this
        # cycle, see noble_escort_reserve().
        self.troop_reserve = {}

    @staticmethod
    def internet_online():
        """
        Checks whether the bot has internet access
        """
        try:
            # Neutral connectivity probe; intentionally not the upstream repo.
            requests.get("https://www.google.com", timeout=(10, 60))
            return True
        except requests.Timeout:
            return False

    def manual_config(self):
        """
        Runs through manual steps of configuring the bot
        """
        logging.info(
            "Hello and welcome, it looks like you don't have a config file (yet)"
        )
        if not FileManager.path_exists("config.example.json"):
            logging.error(
                "Oh no, config.example.json and config.json do not exist. You broke something didn't you?"
            )
            return False
        logging.info(
            "Please enter the current (logged-in) URL of the world you are playing on (or q to exit)"
            "The URL should look something like this:\n"
            "https://nl01.tribalwars.nl/game.php?village=12345&screen=overview"
        )
        input_url = input("URL: ")
        if input_url.strip() == "q":
            return False
        server = input_url.split("://")[1].split("/")[0]
        game_endpoint = input_url.split("?")[0]
        sub_parts = server.split(".")[0]
        logging.info("Game endpoint: %s", game_endpoint)
        logging.info("World: %s", sub_parts.upper())
        check = input("Does this look correct? [nY]")
        if "y" in check.lower():
            browser_ua = input(
                "Enter your browser user agent "
                "(to lower detection rates). Just google what is my user agent> "
            )
            if browser_ua and len(browser_ua) < 10:
                logging.error(
                    "It should start with Chrome, Firefox or something. Please try again"
                )
                return self.manual_config()
            browser_ua = browser_ua.strip()
            disclaimer = """
            Read carefully: Please note the use of this bot can cause bans, kicks, annoyances and other stuff.
            I do my best to make the bot as undetectable as possible but most issues / bans are config related.
            Make sure you keep your bot sleeps at a reasonable numbers and please don't blame me if your account gets banned ;) 
            PS. make sure to regularly (1-2 per day) logout/login using the browser session and supply the new cookie string. 
            Using a single session for 24h straight will probably result in a ban
            """
            logging.info(disclaimer)
            final_check = input(
                "Do you understand this and still wish to continue, please type: yes and press enter> "
            )
            if "yes" not in final_check.lower():
                logging.info("Goodbye :)")
                sys.exit(0)

            template = FileManager.load_json_file("config.example.json", object_pairs_hook=collections.OrderedDict)
            if not template:
                logging.error("Unable to open config.example.json")
                return False
            template["server"]["endpoint"] = game_endpoint
            template["server"]["server"] = sub_parts.lower()
            template["bot"]["user_agent"] = browser_ua

            FileManager.save_json_file(template, "config.json")
            print("Deployed new configuration file")
            return True

        print("Make sure your url starts with https:// and contains the game.php? part")
        return self.manual_config()

    def config(self):
        """
        Fetches the config file
        Or the example one of it doesn't exist
        Also updates config file with template data in case of an update
        """
        template = FileManager.load_json_file("config.example.json")

        if not FileManager.path_exists("config.json"):
            # The console wizard is opt-in (twb.py --setup): its questions are
            # answered better by the dashboard's Add-world form, which also takes
            # the cookie - and a console truncates a pasted cookie. Waiting for
            # the dashboard also means a bot started before its world exists
            # adopts that world instead of walking the user through building a
            # second config for the same account, which is how two bots end up
            # on one account logging each other out (core/instance_lock.py).
            if "--setup" in sys.argv:
                if not self.manual_config():
                    print("No config file found. Exiting")
                    sys.exit(1)
            elif not wait_for_world_setup():
                sys.exit(1)
            return self.config()

        config = FileManager.load_json_file("config.json", object_pairs_hook=collections.OrderedDict)

        if template and config["build"]["version"] != template["build"]["version"]:
            print(
                "Outdated config file found, merging (old copy saved as config.bak)\n"
                "Remove config.example.json to disable this behavior"
            )
            FileManager.copy_file("config.json", "config.bak")

            config = self.merge_configs(config, template)
            FileManager.save_json_file(config, "config.json")

            print("Deployed new configuration file")

        return config

    @staticmethod
    def merge_configs(old_config, new_config):
        """
        Merges sections of two config files, always ensuring the last version
        """
        to_ignore = ["villages", "build"]
        for section in old_config:
            if section not in to_ignore:
                for entry in old_config.get(section, {}):
                    if entry in new_config.get(section, {}):
                        new_config[section][entry] = old_config[section][entry]
        villages = collections.OrderedDict()
        for v in old_config["villages"]:
            nc = new_config["village_template"]
            vdata = old_config["villages"][v]
            for entry in nc:
                if entry not in vdata:
                    vdata[entry] = nc[entry]
            villages[v] = vdata
        new_config["villages"] = villages
        return new_config

    def get_overview(self, config):
        """
        Gets the overview page to automatically detect world options and owned villages
        """
        overview_page = OverviewPage(self.wrapper)
        logged_in = bool(Extractor.game_state(overview_page.result_get))
        if not logged_in:
            # The session/cookie expired: the overview is a login page with no
            # villages. Try a non-blocking re-auth from cache/cookies.txt (picks up
            # a freshly dropped cookie automatically), then re-fetch once.
            if self.wrapper.reauth():
                overview_page = OverviewPage(self.wrapper)
                logged_in = bool(Extractor.game_state(overview_page.result_get))

        was_logged_out = self.session_logged_out
        self.session_logged_out = not logged_in

        if not logged_in:
            # Do NOT wipe found_villages here: the villages are not gone, the bot is
            # just logged out. Keeping the previous list (and the session_logged_out
            # flag) stops the false "village is not available anymore" and lets the
            # run loop skip the cycle instead of acting on a dead session.
            print(
                "Overview could not be read: the session looks logged out (cookie "
                "expired). Villages are NOT lost - refresh the cookie "
                "(cache/cookies.txt)."
            )
            if not was_logged_out:  # notify once per logout, not every cycle
                Notification.send(
                    "TWB: main bot session is logged out (cookie expired). "
                    "Villages are not lost; refresh the cookie to resume.",
                    category="session",
                )
            return overview_page, config

        if was_logged_out:
            print("Session restored - resuming normal operation")

        self.check_server_clock(overview_page)

        # Prefer the already-parsed villages_data (BS4-based, position-stable).
        # Fall back to the regex extractor if the table parse returned nothing.
        if overview_page.villages_data:
            parsed = list(overview_page.villages_data.keys())
        else:
            parsed = Extractor.village_ids_from_overview(overview_page.result_get.text)

        if parsed:
            if self._village_parse_failed:
                print("Overview parsing recovered - villages read again")
                self._village_parse_failed = False
            self.found_villages = parsed
        else:
            # Logged in (valid game state) but not a single village parsed. A
            # logged-in player always owns at least one village, so this is a
            # parse failure (e.g. the overview markup changed), not an empty
            # account. Overwriting found_villages with [] would make the run loop
            # skip every village as "not available anymore" while the bot looks
            # healthy. Keep the previous list and warn; retry next cycle. The
            # per-village run re-fetches each village's own page independently, so
            # continuing to manage the known villages is safe.
            was_failed = self._village_parse_failed
            self._village_parse_failed = True
            logging.getLogger("twb").warning(
                "Overview parsed zero villages while logged in - treating as a "
                "parse failure and keeping the previous %d village(s). The "
                "overview page markup may have changed.", len(self.found_villages)
            )
            if not was_failed:
                Notification.send(
                    "TWB: could not read any villages from the overview while "
                    "logged in (possible page change). Keeping the known village "
                    "list and retrying; villages are NOT lost.",
                    category="village",
                )
            # found_villages is intentionally left unchanged; skip prune below too.
            return overview_page, config
        if config["bot"].get("add_new_villages", False):
            for found_vid in self.found_villages:
                if found_vid not in config["villages"]:
                    print(
                        f"Village {found_vid} was found but no config entry was found. Adding automatically"
                    )
                    config = self.add_village(village_id=found_vid)

        # Forget villages that are no longer owned (conquered/nobled) so their
        # stale state stops surfacing on the dashboard.
        config = self.prune_lost_villages(config)

        # Cache account-wide "op pad" (moving) troops so the dashboard can split
        # away troops into support (in other villages) vs in transit.
        self.update_troop_movements()
        self.update_troop_templates()

        return overview_page, config

    def check_server_clock(self, overview_page):
        """Track the game server's clock, and warn once if the host disagrees.

        Two separate disagreements matter here, and they need different sources:

        * The host clock has drifted in absolute terms. Epoch timestamps differ,
          so scheduled attacks (which wait on `time.time()`) launch off-target.
          `time_generated` in the page game data catches this.
        * The host runs in another timezone. Epoch timestamps are then *identical*
          and this check sees nothing wrong, but every wall-clock window a player
          writes (forced peace, arrival times) means a different instant to us
          than it did to them. Only the server's own displayed clock catches it,
          which is what ServerClock samples below.
        """
        # Sampled every cycle, not once: it is how forced-peace windows are
        # anchored, and the offset moves when either side enters/leaves DST.
        wall_offset = ServerClock.sample(overview_page.result_get.text)
        if wall_offset is not None and not self._wall_clock_warned \
                and abs(wall_offset) >= self.CLOCK_WALL_WARN_SECONDS:
            self._wall_clock_warned = True
            msg = (
                "Host wall clock is %.1f hours off the game server (server reads "
                "%s). Forced-peace windows are anchored to the server's clock, so "
                "they stay correct, but times you type into the dashboard are read "
                "in your browser's timezone - set the host and browser to the "
                "world's timezone to keep them the same."
                % (wall_offset / 3600.0, ServerClock.now().strftime("%H:%M"))
            )
            logging.getLogger("twb").warning(msg)
            Notification.send("TWB: " + msg, category="village")

        if self._clock_checked:
            return
        game_data = Extractor.game_state(overview_page.result_get)
        generated = (game_data or {}).get("time_generated")
        if not generated:
            return  # no server timestamp on this page; try again next cycle
        self._clock_checked = True
        try:
            server_ts = float(generated) / 1000.0
        except (TypeError, ValueError):
            return
        skew = time.time() - server_ts
        if abs(skew) > self.CLOCK_SKEW_WARN_SECONDS:
            msg = (
                "Host clock differs from the game server by %d seconds (%.1f min). "
                "Forced-peace windows and scheduled attacks are timed against the "
                "host clock and will be offset by this amount - set the host's "
                "timezone/clock to match the server." % (int(skew), skew / 60.0)
            )
            logging.getLogger("twb").warning(msg)
            Notification.send("TWB: " + msg, category="village")
        else:
            logging.getLogger("twb").info(
                "Host clock is within %ds of server time (skew %ds)",
                self.CLOCK_SKEW_WARN_SECONDS, int(skew)
            )

    def update_troop_movements(self):
        """Cache troop locations the per-village snapshot cannot tell apart:
        'op pad' (moving / in transit) and 'elders' (support stationed in other
        villages). Read straight from the game so the dashboard never has to
        derive support from mismatched snapshots.

        The type=complete overview carries all of it in one page, per village,
        so that is what we ask for; the older per-type pages are only a fallback
        for when that table cannot be parsed."""
        if not self.found_villages:
            return
        # Skip the extra GET while the cached split is still fresh; the
        # dashboard tolerates slightly stale movement data.
        existing = FileManager.load_json_file("cache/troops_moving.json")
        if existing and int(time.time()) - int(existing.get("when", 0) or 0) < self.TROOP_MOVE_REFRESH_SECONDS:
            return
        vid = self.found_villages[0]
        base = f"game.php?village={vid}&screen=overview_villages&mode=units&type="
        try:
            # page=-1 keeps every village on one page once the account outgrows
            # the overview's default page size.
            page = self.wrapper.get_url(base + "complete&page=-1")
            by_village = Extractor.units_overview_complete(page) if page else {}
            if by_village:
                def total(key):
                    out = {}
                    for village in by_village.values():
                        for unit, count in (village.get(key) or {}).items():
                            out[unit] = out.get(unit, 0) + count
                    return out

                stamp = int(time.time())
                FileManager.save_json_file({
                    "moving": total("moving"),
                    "support": total("elsewhere"),
                    "home": total("own"),
                    "by_village": by_village,
                    "when": stamp,
                    # When the by_village breakdown was last read for real, so a
                    # later partial write cannot pass stale detail off as fresh.
                    "complete_when": stamp,
                    "partial": False,
                }, "cache/troops_moving.json")
                return
            mv = self.wrapper.get_url(base + "moving")
            sup = self.wrapper.get_url(base + "away")
            # Never clobber a good reading with a partial one. The per-type
            # pages carry no per-village breakdown and no "home" figure, so
            # writing them alone would strip both - and every consumer that
            # asks "where do this village's troops stand" would silently fall
            # back to the per-village snapshot, which only knows what is
            # standing at home. Carry the last complete reading forward and
            # mark the write partial instead.
            payload = {
                "moving": Extractor.units_overview(mv) if mv else {},
                "support": Extractor.units_overview(sup) if sup else {},
                "when": int(time.time()),
                "partial": True,
            }
            if existing:
                for key in ("by_village", "home"):
                    if existing.get(key):
                        payload[key] = existing[key]
                payload["complete_when"] = existing.get("complete_when", existing.get("when"))
            FileManager.save_json_file(payload, "cache/troops_moving.json")
        except Exception as exc:
            # Non-critical: the dashboard falls back to lumped "away". Still log
            # at debug so a persistent parse/markup regression is visible.
            logging.getLogger("twb").debug("update_troop_movements failed: %s", exc)

    def update_troop_templates(self):
        """Cache the player's rally-point troop templates for the dashboard.

        They are what the player already set up in-game ("OFF", "Fake", ...),
        so the Attack tab can offer them instead of asking for the same unit
        list to be typed again. Templates are account-wide and rarely edited,
        hence the long refresh."""
        if not self.found_villages:
            return
        existing = FileManager.load_json_file("cache/troop_templates.json")
        if existing and int(time.time()) - int(existing.get("when", 0) or 0) < self.TEMPLATE_REFRESH_SECONDS:
            return
        vid = self.found_villages[0]
        try:
            page = self.wrapper.get_url(
                f"game.php?village={vid}&screen=place&target_type=coord")
            templates = Extractor.troop_templates(page) if page else {}
            if templates:
                FileManager.save_json_file(
                    {"templates": templates, "when": int(time.time())},
                    "cache/troop_templates.json")
        except Exception as exc:
            # Cosmetic feature: the dashboard just offers no templates.
            logging.getLogger("twb").debug("update_troop_templates failed: %s", exc)

    def add_village(self, village_id, template=None):
        """
        Adds a new village and sets the default template data
        """
        original = self.config()
        FileManager.copy_file("config.json", "config.bak")

        if not template and "village_template" not in original:
            print(f"Village entry {village_id} could not be added to the config file!")
            return

        original["villages"][village_id] = template if template else original["village_template"]

        FileManager.save_json_file(original, "config.json")
        print("Deployed new configuration file")
        return original

    def prune_lost_villages(self, config):
        """Forget villages that are no longer owned (conquered / nobled).

        Only runs when logged in with a non-empty found_villages list, so a
        logged-out or failed overview can never wipe still-owned villages. For
        each lost village it drops the config entry, deletes the cached state
        (so the dashboard stops showing the village, its troops and resources)
        and stops managing it. config.json is backed up to config.bak first.
        """
        if not self.found_villages:
            return config
        lost = [vid for vid in list(config.get("villages", {}).keys())
                if vid not in self.found_villages]
        if not lost:
            return config

        FileManager.copy_file("config.json", "config.bak")
        for vid in lost:
            print("Village %s is no longer owned (conquered/nobled) - removing it" % vid)
            Notification.send(
                "TWB: village %s was lost (conquered/nobled). Removing it from the bot." % vid,
                category="village")
            config["villages"].pop(vid, None)
            FileManager.remove_file("cache/managed/%s.json" % vid)
            # Drop it from the in-memory managed list so the run loop stops touching it.
            self.villages = [v for v in self.villages if str(v.village_id) != str(vid)]
        FileManager.save_json_file(config, "config.json")
        print("Deployed new configuration file (removed %d lost village(s))" % len(lost))
        return config

    @staticmethod
    def get_world_options(overview_page: OverviewPage, config):
        """
        Detects world options like flags and knight enabled from the overview page
        """

        def check_and_set(option_key, setting, check_string=None):
            nonlocal changed
            if world_config[option_key] is None:
                world_config[option_key] = setting
                if check_string:
                    world_config[option_key] = check_string in overview_page.result_get.text

                changed = True

        changed = False
        world_settings = overview_page.world_settings
        world_config = config["world"]

        check_and_set("flags_enabled", world_settings.flags)
        check_and_set("knight_enabled", world_settings.knight)
        check_and_set("boosters_enabled", world_settings.boosters)
        check_and_set("quests_enabled", world_settings.quests, "Quests.setQuestData")

        return changed, config

    @staticmethod
    def is_active_hours(config):
        """
        Checks if the bot is within active hours
        Allows the bot to run more productive during an active session and ensure stealth at night
        Bounds may be whole hours ("6-23") or HH:MM ("5-23:30").
        """

        def to_minutes(bound, is_end):
            if ":" in bound:
                hour, minute = bound.split(":")
                return int(hour) * 60 + int(minute)
            # A whole-hour end bound is inclusive ("6-23" is active
            # 06:00-23:59, matching how a user reads "6 to 23").
            return int(bound) * 60 + (59 if is_end else 0)

        raw_start, raw_end = config["bot"]["active_hours"].split("-")
        start = to_minutes(raw_start.strip(), is_end=False)
        end = to_minutes(raw_end.strip(), is_end=True)
        now = time.localtime()
        now_m = now.tm_hour * 60 + now.tm_min
        if start <= end:
            return start <= now_m <= end
        # Overnight window that wraps past midnight (e.g. "22-6"): active from
        # the start bound through the end bound inclusive.
        return now_m >= start or now_m <= end

    def in_quiet_hours(self, config=None):
        """True while the account is meant to look asleep.

        One definition for the two places that need it: the main loop, which
        decides whether to sleep the cycle away, and the captcha wait, which
        decides how often to re-check a blocked page (core/request.py's
        _captcha_poll_interval).

        The main loop passes its cycle config. The captcha wait passes nothing
        and gets a fresh read, because it may have been blocked for hours and an
        active_hours edit made in the dashboard meanwhile should count.
        """
        if config is None:
            config = self.config()
        if config["bot"].get("inactive_still_active", False):
            return False
        return not self.is_active_hours(config=config)

    def _make_poller_wrapper(self, config):
        """A separate, GET-only web session for the incoming-attack poller.

        It runs in its own thread, so it gets its own WebWrapper (and requests
        session) to avoid racing the main loop's per-request state. Cookies are
        reloaded from the cached session each cycle, so a refreshed login is
        picked up without restarting the bot.
        """
        poller = WebWrapper(
            config["server"]["endpoint"],
            server=config["server"]["server"],
            endpoint=config["server"]["endpoint"],
        )
        poller.block_on_captcha = False  # never input() from a background thread
        if config["bot"].get("user_agent"):
            poller.headers["user-agent"] = config["bot"]["user_agent"]
        return poller

    def incoming_poller(self, config):
        """Background loop: track incoming attacks on their own short cadence.

        Detection accuracy of the auto-tag depends on how soon after an attack
        is sent we first see it (for adjacent villages ram vs. noble is only a
        few minutes apart), so this polls far more often than the main run loop
        and independently of it.
        """
        logger = logging.getLogger("Incomings")
        poller = self._make_poller_wrapper(config)
        low = int(config["bot"].get("incoming_check_min", 300))
        high = int(config["bot"].get("incoming_check_max", 570))
        first = True
        while self.should_run:
            # Poll immediately on the first pass instead of sleeping first - a
            # restart otherwise leaves the dashboard's live incomings cache
            # empty (and trusted as authoritative) for up to `high` seconds,
            # during which an actual attack silently doesn't show.
            if first:
                first = False
            else:
                time.sleep(random.randint(low, high))
                if not self.should_run:
                    break
            # Deliberately NOT gated on the activity window. Attack detection
            # is the one thing that still matters while the account is meant to
            # look asleep: an attack landing at 04:00 is exactly the one you
            # cannot afford to first hear about at 05:00. Set `incoming_check`
            # to false to turn this loop off entirely.
            try:
                session = FileManager.load_json_file("cache/session.json")
                if session and session.get("cookies"):
                    poller.web.cookies.update(session["cookies"])
                target = next(iter(config["villages"]), None)
                if not target:
                    continue
                IncomingManager(village_id=target, wrapper=poller).run()
            except Exception as exc:
                logger.warning("Incoming poll failed: %s", exc)

    def scheduled_attack_runner(self, config):
        """Background loop: fire timed attacks queued from the Attack tab.

        Runs on its own clock (not the slow main cycle) so commands launch close
        to their scheduled moment. We wake `prestage` seconds before a command's
        send moment, then run the open+confirm steps and fire the final launch at
        arrival - (server travel time) - network_lead for accuracy. Cookies are
        reloaded from the cached session each time so a refreshed login is picked
        up automatically.
        """
        logger = logging.getLogger("AttackScheduler")
        sender = self._make_poller_wrapper(config)
        # Timed sends must not incur the wrapper's 3-7s human-pacing delay, or the
        # open->confirm->launch sequence lands the attack tens of seconds late.
        sender.priority_mode = True
        prestage = float(config["bot"].get(
            "sched_prestage_seconds", attack_scheduler.PRESTAGE_SECONDS))
        network_lead = float(config["bot"].get(
            "sched_lead_seconds", attack_scheduler.NETWORK_LEAD))
        last_prune = 0
        while self.should_run:
            try:
                now = time.time()
                if now - last_prune > 3600:
                    attack_scheduler.prune()
                    last_prune = now
                next_send = attack_scheduler.next_send_ts(lead=prestage)
                if next_send is None:
                    time.sleep(2)
                    continue
                wait = next_send - prestage - now
                # Re-check the queue at least every 2s so new/cancelled commands
                # are picked up; sleep until the pre-stage window when imminent.
                if wait > 2:
                    time.sleep(2)
                    continue
                if wait > 0:
                    time.sleep(wait)
                if not self.should_run:
                    break
                session = FileManager.load_json_file("cache/session.json")
                if session and session.get("cookies"):
                    sender.web.cookies.update(session["cookies"])
                attack_scheduler.run_due(sender, lead=prestage, network_lead=network_lead)
            except Exception as exc:
                logger.warning("Scheduled attack runner error: %s", exc)
                time.sleep(2)

    def csnipe_runner(self, config):
        """Background loop: execute armed cancel-snipes from the Defense tab.

        A snipe occupies its runner from the send until the cancel (up to ~10
        minutes), so it gets its own thread and wrapper instead of sharing the
        scheduled-attack runner - a c-snipe must never delay a timed attack or
        vice versa. Same wake-up pattern as the scheduler otherwise, and
        priority_mode for the same reason: millisecond sends can't afford the
        wrapper's human-pacing delay.
        """
        logger = logging.getLogger("CSnipe")
        sender = self._make_poller_wrapper(config)
        sender.priority_mode = True
        network_lead = float(config["bot"].get(
            "sched_lead_seconds", attack_scheduler.NETWORK_LEAD))
        last_prune = 0
        while self.should_run:
            try:
                now = time.time()
                if now - last_prune > 3600:
                    csnipe.prune()
                    last_prune = now
                next_start = csnipe.next_start_ts()
                if next_start is None or next_start - now > 2:
                    time.sleep(2)
                    continue
                if next_start > now:
                    time.sleep(next_start - now)
                if not self.should_run:
                    break
                session = FileManager.load_json_file("cache/session.json")
                if session and session.get("cookies"):
                    sender.web.cookies.update(session["cookies"])
                csnipe.run_due(sender, network_lead=network_lead)
            except Exception as exc:
                logger.warning("C-snipe runner error: %s", exc)
                time.sleep(2)

    def snipe_runner(self, config):
        """Background loop: execute armed support-snipes from the Defense tab.

        Mirrors the c-snipe runner: its own thread + wrapper (a snipe occupies
        the runner from claim to send, and must never delay a timed attack),
        priority_mode so the ms-precise launch skips the human-pacing delay.
        """
        logger = logging.getLogger("Snipe")
        sender = self._make_poller_wrapper(config)
        sender.priority_mode = True
        network_lead = float(config["bot"].get(
            "sched_lead_seconds", attack_scheduler.NETWORK_LEAD))
        last_prune = 0
        while self.should_run:
            try:
                now = time.time()
                if now - last_prune > 3600:
                    snipe.prune()
                    last_prune = now
                next_start = snipe.next_start_ts()
                if next_start is None or next_start - now > 2:
                    time.sleep(2)
                    continue
                if next_start > now:
                    time.sleep(next_start - now)
                if not self.should_run:
                    break
                session = FileManager.load_json_file("cache/session.json")
                if session and session.get("cookies"):
                    sender.web.cookies.update(session["cookies"])
                snipe.run_due(sender, network_lead=network_lead)
            except Exception as exc:
                logger.warning("Snipe runner error: %s", exc)
                time.sleep(2)

    def noble_escort_reserve(self, config):
        """Troops the armed noble jobs will need at the end of this cycle,
        as {village_id: {unit: count}}.

        Runs before anything that spends troops (player farms, and per village
        the barb shaper, scavenging and the farm pass) so the escort is still
        home when run_noble_barbs() gets there. Cache-only, no requests. Jobs
        whose escort trigger is not met yet reserve nothing, so a job waiting
        for troops never keeps scavenging idle."""
        farms = config.get("farms", {})
        if not farms.get("noble_barb", True) or \
                not farms.get("noble_escort_reserve", True):
            return {}
        try:
            # Live troop counts (one rally point request per sending village):
            # the managed cache is written after that village's farm pass, so
            # it under-reports exactly the units an escort competes for.
            reserve = escort_reservations(
                wrapper=self.wrapper,
                focus=farms.get("noble_focus_fire", True))
        except Exception as exc:
            logging.getLogger("NobleBarb").warning(
                "Escort reservation failed: %s", exc)
            return {}
        for village_id, units in reserve.items():
            print("Reserving %s in village %s for an armed noble job"
                  % (units, village_id))
        return reserve

    def run_player_farms(self, config):
        """One player-farm pass (curated hit list, report-driven auto-stop).

        With farms.player_farm_priority (default on) this runs BEFORE the
        village loop, so the hit list gets first claim on the light cavalry -
        player farms are the more consistent income, barbs are contested."""
        if not config.get("farms", {}).get("player_farm", True):
            return
        try:
            PlayerFarmManager(wrapper=self.wrapper, config=config,
                              reserve=self.troop_reserve).run()
        except Exception as exc:
            logging.getLogger("PlayerFarm").warning(
                "Player farm pass failed: %s", exc)

    def run_noble_barbs(self, config):
        """One auto-noble pass (alpha).

        Runs twice per cycle: once at the top, so an armed job fires within a
        minute of the cycle starting instead of waiting out every village (the
        pass reads the sending village's rally point live, so it no longer
        needs the end-of-cycle troop snapshot), and once after the village
        loop, which catches jobs whose escort only came home mid-cycle. Jobs
        that already sent are held by their in_flight guard, so the second
        pass is a no-op for them."""
        if not config.get("farms", {}).get("noble_barb", True):
            return
        try:
            NobleBarbManager(wrapper=self.wrapper, config=config).run()
        except Exception as exc:
            logging.getLogger("NobleBarb").warning(
                "Noble-barb pass failed: %s", exc)

    def heartbeat(self, sleeping=False):
        """Stamp proof that the main loop is still turning.

        `sleeping` marks the deliberate overnight pause, so a watchdog can tell
        "idle on purpose" apart from "hung".

        Called as the loop makes progress - not just once per cycle. A full
        cycle over several villages takes far longer than the watchdog's
        threshold (which only budgets for the configured sleep), so stamping
        once at the top made a healthy multi-village bot look hung for the
        back half of every cycle.
        """
        FileManager.save_json_file_atomic(
            {"ts": int(time.time()), "runs": self.runs, "started": self.started,
             "sleeping": bool(sleeping)},
            "cache/heartbeat.json")

    def sleep_through_inactive_hours(self, config):
        """Idle until the activity window reopens, doing nothing at all.

        This is what `inactive_still_active: false` is supposed to mean. The
        point is not to save requests, it is that the account should look like
        a player who went to bed: a human does not start a scavenge run at
        02:40 or keep a build queue topped up all night, and doing so is the
        pattern that makes a bot a bot.

        The heartbeat keeps ticking (flagged as sleeping) so the watchdog does
        not read a deliberate pause as a hang, and the wait is chunked so a
        stop signal is honoured within the minute rather than at dawn.
        """
        chunk = 60
        announced = False
        while self.should_run:
            if self.is_active_hours(config=config):
                if announced:
                    print("Waking up - back inside active hours.")
                return
            if not announced:
                logging.getLogger("twb").info(
                    "Outside active hours (%s) and inactive_still_active is "
                    "off - idling until the window reopens",
                    config["bot"].get("active_hours"))
                print("Sleeping: outside active hours, nothing but incoming "
                      "attack checks will run.")
                announced = True
            self.heartbeat(sleeping=True)
            time.sleep(chunk)
            # Re-read now and then so an active_hours edit in the dashboard is
            # picked up tonight instead of tomorrow.
            self.slept_chunks = getattr(self, "slept_chunks", 0) + 1
            if self.slept_chunks % 10 == 0:
                config = self.config()

    def run(self):
        """
        Run the bot
        TODO: make less messy
        """
        config = self.config()
        # One bot per account. A second instance on the same account does not
        # run twice, it knocks the first one's session out (see
        # core/instance_lock.py) - one bot keeps playing while the other logs
        # "session looks logged out (cookie expired)" every cycle forever.
        holder = InstanceLock.acquire(config["server"]["endpoint"], world=ACTIVE_WORLD)
        if holder:
            print(
                "Another bot is already running for %s: %s.\n"
                "Two bots on one account log each other out - not starting a "
                "second one. Stop the other bot first (or use --world for a "
                "different account)." % (
                    config["server"]["server"], InstanceLock.describe(holder))
            )
            sys.exit(1)
        # Only announce a start that is actually happening - a refused duplicate
        # start should not push a "starting up" notification.
        Notification.send("TWB is starting up", category="startup")
        if not self.internet_online():
            print("Internet seems to be down, waiting till its back online...")
            sleep = 0
            if self.is_active_hours(config=config):
                sleep = config["bot"]["active_delay"]
            else:
                if config["bot"]["inactive_still_active"]:
                    sleep = config["bot"]["inactive_delay"]

            sleep += random.randint(20, 120)
            dtn = datetime.datetime.now()
            dt_next = dtn + datetime.timedelta(0, sleep)
            print(
                "Dead for %.2f minutes (next run at: %s)" % (sleep / 60, dt_next.time())
            )
            time.sleep(sleep)
            return False

        self.wrapper = WebWrapper(
            config["server"]["endpoint"],
            server=config["server"]["server"],
            endpoint=config["server"]["endpoint"],
            reporter_enabled=config["reporting"]["enabled"],
            reporter_constr=config["reporting"]["connection_string"],
        )

        # Lets the captcha wait slow its re-checks down overnight instead of
        # polling a blocked session every 20s until someone wakes up.
        self.wrapper.quiet_hours_check = self.in_quiet_hours
        self.wrapper.start()
        # A fresh process can't already be inside the captcha-wait loop that owns
        # this marker (core/request.py's _await_captcha_clear), so any leftover
        # file here is necessarily orphaned by a previous instance that was
        # killed/restarted while blocked - clear it or the dashboard is stuck
        # reporting "captcha" forever despite a perfectly healthy heartbeat.
        FileManager.remove_file(WebWrapper.CAPTCHA_BLOCK_FILE)
        if not config["bot"].get("user_agent", None):
            print(
                "No custom user agent was supplied, this will likely get you banned."
                "Please set the bot -> user_agent parameter to your browsers one. "
                "Just google what is my user agent"
            )
            return
        self.wrapper.headers["user-agent"] = config["bot"]["user_agent"]
        for vid in config["villages"]:
            v = Village(wrapper=self.wrapper, village_id=vid)
            self.villages.append(copy.deepcopy(v))
        # setup additional builder
        rm = None
        defense_states = {}
        if config["bot"].get("incoming_check", True):
            poller_thread = threading.Thread(
                target=self.incoming_poller, args=(config,), daemon=True
            )
            poller_thread.start()
            print("Incoming-attack poller started (every %d-%ds)" % (
                int(config["bot"].get("incoming_check_min", 300)),
                int(config["bot"].get("incoming_check_max", 570)),
            ))
        if config["bot"].get("scheduled_attacks", True):
            sched_thread = threading.Thread(
                target=self.scheduled_attack_runner, args=(config,), daemon=True
            )
            sched_thread.start()
            print("Scheduled-attack runner started")
        if config["bot"].get("csnipe", True):
            csnipe_thread = threading.Thread(
                target=self.csnipe_runner, args=(config,), daemon=True
            )
            csnipe_thread.start()
            print("Cancel-snipe runner started")
        if config["bot"].get("snipe", True):
            snipe_thread = threading.Thread(
                target=self.snipe_runner, args=(config,), daemon=True
            )
            snipe_thread.start()
            print("Support-snipe runner started")
        while self.should_run:
            # Heartbeat: proof the main loop is still turning, independent of the
            # incoming-attack poller and scheduler threads, which run on their own
            # non-blocking wrappers and keep logging even when this loop is stuck
            # (e.g. waiting out a captcha in WebWrapper._await_captcha_clear).
            # OverviewBuilder uses staleness here as a generic "bot stalled" signal.
            self.heartbeat()
            # A sleeping player does nothing at all - see
            # sleep_through_inactive_hours. Checked before the network so a
            # bot that wakes into the dark goes straight back to sleep.
            if self.in_quiet_hours(config):
                self.sleep_through_inactive_hours(config)
                config = self.config()
                continue
            if not self.internet_online():
                print("Internet seems to be down, waiting till its back online...")
                sleep = 0
                if self.is_active_hours(config=config):
                    sleep = config["bot"]["active_delay"]
                else:
                    if config["bot"]["inactive_still_active"]:
                        sleep = config["bot"]["inactive_delay"]

                sleep += random.randint(20, 120)
                dtn = datetime.datetime.now()
                dt_next = dtn + datetime.timedelta(0, sleep)
                print(
                    "Dead for %.2f minutes (next run at: %s)" % (sleep / 60, dt_next.time())
                )
                time.sleep(sleep)
            else:
                config = self.config()
                overview_page, config = self.get_overview(config)
                if self.session_logged_out:
                    # Logged out: don't run villages on a dead session. Wait a
                    # normal cycle and retry (a refreshed cookie auto-recovers via
                    # reauth() in get_overview).
                    sleep = (
                        config["bot"]["active_delay"]
                        if self.is_active_hours(config=config)
                        else config["bot"]["inactive_delay"]
                    )
                    sleep += random.randint(20, 120)
                    dt_next = datetime.datetime.now() + datetime.timedelta(0, sleep)
                    print(
                        "Session logged out - waiting %.1f min before retrying "
                        "(next run at: %s)" % (sleep / 60, dt_next.time())
                    )
                    time.sleep(sleep)
                    continue
                has_changed, new_cf = self.get_world_options(overview_page, config)
                if has_changed:
                    print("Updated world options")
                    config = self.merge_configs(config, new_cf)
                    FileManager.save_json_file(config, "config.json")
                    print("Deployed new configuration file")

                known_village_ids = [v.village_id for v in self.villages]
                for vid in config["villages"]:
                    if vid not in known_village_ids:
                        print("Village %s was newly added, registering it for management" % vid)
                        v = Village(wrapper=self.wrapper, village_id=vid)
                        self.villages.append(copy.deepcopy(v))

                # Claim the daily login bonus once per day, only during active
                # hours: a chest opened at the same minute past midnight every
                # night is a robotic pattern, a morning claim is what a human
                # session looks like.
                if config["bot"].get("claim_daily_bonus", False) and \
                        self.is_active_hours(config=config):
                    try:
                        dailybonus.run(
                            self.wrapper, next(iter(config["villages"]), None))
                    except Exception as exc:
                        logging.getLogger("DailyBonus").warning(
                            "Daily bonus check failed: %s", exc)

                # Nobles first: nothing has spent a troop yet this cycle, so a
                # job that is ready goes out now instead of 20 minutes later.
                self.run_noble_barbs(config)
                # Then hold back the escorts of the jobs that did NOT send (in
                # flight, or still short) so the rest of the cycle leaves them
                # alone and the pass after the village loop can still fire.
                self.troop_reserve = self.noble_escort_reserve(config)

                if config.get("farms", {}).get("player_farm_priority", True):
                    self.run_player_farms(config)

                village_number = int(
                    config["bot"].get("village_name_number_start", 1) or 1)
                for village in self.villages:
                    if village.village_id not in self.found_villages:
                        print(
                            "Village %s will be ignored because it is not available anymore"
                            % village.village_id
                        )
                        continue
                    if not rm:
                        rm = village.rep_man
                    else:
                        village.rep_man = rm
                    if (
                            "auto_set_village_names" in config["bot"]
                            and config["bot"]["auto_set_village_names"]
                    ):
                        template = config["bot"]["village_name_template"]
                        fs = (
                                "%0"
                                + str(config["bot"]["village_name_number_length"])
                                + "d"
                        )
                        num_pad = fs % village_number
                        template = template.replace("{num}", num_pad)
                        village.village_set_name = template

                    village.troop_reserve = self.troop_reserve.get(
                        str(village.village_id), {})
                    # The recruiter counts troops standing in other villages
                    # from this cache, and a full cycle takes far longer than
                    # its refresh window - so top it up here rather than once
                    # per cycle. Costs nothing while the reading is still fresh.
                    self.update_troop_movements()
                    # Same reason: a template edited in-game should reach the
                    # dashboard within minutes, not at the next cycle start.
                    self.update_troop_templates()
                    village.run(config=config)
                    # Each village is minutes of work; stamp as we go so the
                    # watchdog sees progress instead of one silent gap.
                    self.heartbeat()

                    if (
                            village.get_config(
                                section="units", parameter="manage_defence", default=False
                            )
                            and village.def_man
                    ):
                        defense_states[village.village_id] = (
                            village.def_man.under_attack
                            if village.def_man.allow_support_recv
                            else False
                        )
                    village_number += 1

                if len(defense_states) and config["farms"]["farm"]:
                    for village in self.villages:
                        print("Syncing attack states")
                        village.def_man.my_other_villages = defense_states

                if not config.get("farms", {}).get("player_farm_priority", True):
                    self.run_player_farms(config)

                # Second pass: escorts that only came home while the village
                # loop was running.
                self.run_noble_barbs(config)

                sleep = 0
                if self.is_active_hours(config=config):
                    sleep = config["bot"]["active_delay"]
                else:
                    if config["bot"]["inactive_still_active"]:
                        sleep = config["bot"]["inactive_delay"]

                sleep += random.randint(20, 120)
                dtn = datetime.datetime.now()
                dt_next = dtn + datetime.timedelta(0, sleep)
                self.runs += 1

                VillageManager.farm_manager(
                    verbose=True,
                    prune_after_days=config["bot"].get("farm_prune_days", 0),
                    # Hand over the reports the villages already loaded rather
                    # than making farm_manager re-read the cache from disk.
                    reports=ReportManager.last_reports or None,
                    clean_reports=config["bot"].get("clean_reports", 0),
                )
                print(
                    "Dead for %.2f minutes (next run at: %s)"
                    % (sleep / 60, dt_next.time())
                )
                sys.stdout.flush()
                # Stamp after the post-cycle work (farm manager, pruning) and
                # before the long sleep, so a healthy idle bot is never older
                # than its own configured delay.
                self.heartbeat()
                time.sleep(sleep)

    def start(self):
        """
        First run, verify if dirctory structure exist
        """
        directories = [
            "cache/attacks",
            "cache/reports",
            "cache/villages",
            "cache/world",
            "cache/logs",
            "cache/managed",
            "cache/hunter",
            "cache/incomings"
        ]
        FileManager.create_directories(directories)

        self.run()


def main():
    """
    Python main entry function
    """
    check_update()
    for _ in range(3):
        t = TWB()
        try:
            t.start()
        except Exception as e:
            t.should_run = False  # signal this instance's background poller to stop
            # An early failure (bad config, etc.) can crash before self.wrapper is
            # assigned; guard it so the except block doesn't raise a secondary
            # AttributeError that escapes the retry loop and skips the notification.
            if t.wrapper and t.wrapper.reporter:
                t.wrapper.reporter.report(0, "TWB_EXCEPTION", str(e))
            print("I crashed :(   %s" % str(e))
            Notification.send("TWB crashed: %s" % str(e), category="crash")
            # Write the full traceback to the rotating log file (cache/twb.log)
            # as well as stderr, so the crash survives the tmux pane / restart.
            logging.getLogger("twb").exception("I crashed :( %s", str(e))
            traceback.print_exc()

    Notification.send("TWB has crashed 3 times, exiting", category="crash")


# While no world is set up, how often to look for one and to repeat the how-to.
SETUP_POLL_SECONDS = 10
SETUP_REMIND_SECONDS = 300


def wait_for_world_setup():
    """Wait for a world to be set up from the dashboard, and adopt it.

    A fresh copy has no config.json, and the console is the wrong place to build
    one: the dashboard - which start.bat/start.sh have already opened - asks the
    same questions in a form, writes worlds/<name>/config.json and seeds the
    cookie in one go, with no console line-length limit to truncate it.

    So instead of running the setup wizard here, wait for that world to appear
    and take it. Setting the bot up then needs no console typing and no restart.
    Returns False when it cannot tell which world was meant.
    """
    message = (
        "No world set up yet.\n"
        "Open the dashboard (http://localhost:5000/ by default) and use 'Add "
        "world': paste the logged-in game URL, your browser's user agent and "
        "your cookie string.\n"
        "Waiting for %s - the bot picks it up by itself, no restart needed.\n"
        "(Prefer a console wizard? Stop this and run: twb.py --setup)"
        % FileManager.get_path("config.json")
    )
    print(message)
    last_reminder = time.time()
    while True:
        if FileManager.path_exists("config.json"):
            return True
        worlds = configured_worlds()
        if ACTIVE_WORLD is None and len(worlds) == 1:
            print("Found world '%s' - using it." % adopt_configured_world())
            return True
        if ACTIVE_WORLD is None and len(worlds) > 1:
            print(
                "Several worlds are set up (%s) - start the one you mean by "
                "name, for example:\n"
                "    start.bat %s      (Windows)\n"
                "    ./start.sh %s     (Linux/macOS/Pi)"
                % (", ".join(worlds), worlds[0], worlds[0])
            )
            return False
        if time.time() - last_reminder > SETUP_REMIND_SECONDS:
            print(message)
            last_reminder = time.time()
        time.sleep(SETUP_POLL_SECONDS)


def adopt_configured_world():
    """Point this process at the single configured world under worlds/.

    A bot started with no --world and no config.json of its own is not tied to
    anything yet, so when a world is set up while it waits it can simply take it
    - the same rule start.bat/start.sh use to pick a world, applied at runtime.
    Returns the world name, or None when there is not exactly one to take.
    """
    global ACTIVE_WORLD
    worlds = configured_worlds()
    if ACTIVE_WORLD is not None or len(worlds) != 1:
        return None
    name = worlds[0]
    data_dir = os.path.join(os.path.dirname(__file__), "worlds", name)
    os.makedirs(os.path.join(data_dir, "cache"), exist_ok=True)
    FileManager.set_data_dir(data_dir)
    ACTIVE_WORLD = name
    # Move the log file along with the data dir, or the dashboard's Bot logs
    # pane (which reads the selected world's twb.log) would stay empty.
    setup_file_logging()
    return name


def configured_worlds():
    """Names of worlds under worlds/ that already have a config.json."""
    wdir = os.path.join(os.path.dirname(__file__), "worlds")
    if not os.path.isdir(wdir):
        return []
    return sorted(
        name for name in os.listdir(wdir)
        if os.path.isfile(os.path.join(wdir, name, "config.json"))
    )


def unknown_world(world):
    """Refuse a --world that has no config, and say what to do instead. Exits.

    A mistyped world name used to be indistinguishable from a new one: the data
    dir was created on the spot, the bot found no config in it and waited for a
    world that nothing was ever going to set up, logging nothing after the
    integrity check. Nothing said the name was wrong - `nl99` and `n199` differ
    by one glyph - and the empty directory stayed behind in worlds/, where it
    also showed up in the dashboard's world switcher as a world you could select
    and then see nothing in.
    """
    print("There is no world called '%s': worlds/%s/config.json does not exist."
          % (world, world))
    known = configured_worlds()
    if known:
        print("Worlds set up here: %s" % ", ".join(known))
        close = difflib.get_close_matches(world, known, n=1, cutoff=0.6)
        if close:
            print("Did you mean '%s'?" % close[0])
    print(
        "To add a world, open the dashboard and use 'Add world' (it takes the "
        "cookie too), or run: twb.py --setup --world %s" % world
    )
    sys.exit(1)


def resolve_world_dir():
    """Honour `--world <name>`: point config.json + cache/ at worlds/<name>/.

    Lets several bot instances share one source tree and dashboard while keeping
    fully separate config, session and cache per world. Must run before any
    config/cache access. With no --world the data dir stays the project root, so
    single-world setups are completely unchanged. Returns the world name or None.

    An unset-up world name is rejected here (see unknown_world) rather than
    created, so a typo cannot leave a stray directory or a bot waiting forever.
    `--setup` is the one way to name a world that does not exist yet, because
    that is a request to build its config.
    """
    global ACTIVE_WORLD
    world = None
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--world" and i + 1 < len(argv):
            world = argv[i + 1]
        elif arg.startswith("--world="):
            world = arg.split("=", 1)[1]
    if not world or not world.strip():
        return None
    # A single path segment only - never escape the worlds/ directory.
    world = os.path.basename(world.strip())
    data_dir = os.path.join(os.path.dirname(__file__), "worlds", world)
    # Check before creating anything: unknown_world() must not leave the very
    # directory behind that it is complaining does not exist.
    if "--setup" not in argv and not os.path.isfile(os.path.join(data_dir, "config.json")):
        unknown_world(world)
    os.makedirs(os.path.join(data_dir, "cache"), exist_ok=True)
    FileManager.set_data_dir(data_dir)
    ACTIVE_WORLD = world
    logging.info("Running world '%s' (data dir: %s)", world, data_dir)
    return world


def self_config_test():
    """
    Checks if the config file consists of valid json if it exists
    """
    file_location = FileManager.get_path("config.json")
    if not os.path.exists(file_location):
        return None
    try:
        with open(file_location, encoding="utf-8") as c_file:
            json.load(c_file)
            return True
    except Exception as e:
        logging.error(e)
        return False


if __name__ == "__main__":
    resolve_world_dir()  # must run before any config/cache access
    setup_file_logging()  # persist output + crashes to worlds/<name>/cache/twb.log
    if "-i" in sys.argv:
        logging.info("Bot integrity check passed")
        check_conf = self_config_test()
        if sys.version_info[0] == 2:
            raise UnsupportedPythonVersion
        if check_conf is True:
            logging.info("Config integrity check passed")
        if check_conf is False:
            logging.error("Config integrity check failed")
            logging.error("It looks like your config file is corrupted and the bot was not able to start.")
            sys.exit(1)
        sys.exit(0)
    main()
