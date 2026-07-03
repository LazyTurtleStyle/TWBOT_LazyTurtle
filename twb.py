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
from core.request import WebWrapper
from game.village import Village
from game.incomings import IncomingManager
from game import attack_scheduler
from manager import VillageManager
from pages.overview import OverviewPage
from core.exceptions import UnsupportedPythonVersion
from core.extractors import Extractor

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
    villages = []
    wrapper = None
    should_run = True
    runs = 0
    found_villages = []
    # Set by get_overview when the overview comes back as a login page; the run
    # loop skips the cycle instead of treating every village as unavailable.
    session_logged_out = False
    # One-shot: compare the host clock to the game server's time once per startup
    # and warn if they diverge (forced-peace windows + scheduled attacks are timed
    # against the host clock, so a skew silently offsets them).
    _clock_checked = False
    # Warn when the host clock differs from server time by more than this (seconds).
    CLOCK_SKEW_WARN_SECONDS = 300
    # True after an overview came back logged-in but with zero parseable villages
    # (a parse failure, not an empty account). Used to notify once per transition.
    _village_parse_failed = False
    # Troop-movement data is dashboard-only and doesn't need per-cycle freshness.
    # Refresh it at most this often to avoid two extra full-page GETs every cycle
    # (cuts request volume and the bot-like request count).
    TROOP_MOVE_REFRESH_SECONDS = 900

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
            if self.manual_config():
                return self.config()

            print("No config file found. Exiting")
            sys.exit(1)

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
                    "Villages are not lost; refresh the cookie to resume."
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
                    "list and retrying; villages are NOT lost."
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

        return overview_page, config

    def check_server_clock(self, overview_page):
        """Warn once if the host clock diverges from the game server's time.

        Forced-peace windows (game.village.check_forced_peace) and scheduled
        attacks are timed against the host's local clock. When the host runs in a
        different timezone or its clock has drifted, those windows and launches
        land at the wrong moment. TribalWars exposes its own time in the page
        game data (time_generated, milliseconds); compare it to the host once per
        startup so a skew is surfaced instead of silently misfiring.
        """
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
            Notification.send("TWB: " + msg)
        else:
            logging.getLogger("twb").info(
                "Host clock is within %ds of server time (skew %ds)",
                self.CLOCK_SKEW_WARN_SECONDS, int(skew)
            )

    def update_troop_movements(self):
        """Cache account-wide troop locations the snapshot can't tell apart:
        'op pad' (moving / in transit) and 'elders' (support stationed in other
        villages). Read straight from the game so the dashboard never has to
        derive support from mismatched snapshots."""
        if not self.found_villages:
            return
        # Skip the two extra GETs while the cached split is still fresh; the
        # dashboard tolerates slightly stale movement data.
        existing = FileManager.load_json_file("cache/troops_moving.json")
        if existing and int(time.time()) - int(existing.get("when", 0) or 0) < self.TROOP_MOVE_REFRESH_SECONDS:
            return
        vid = self.found_villages[0]
        base = f"game.php?village={vid}&screen=overview_villages&mode=units&type="
        try:
            mv = self.wrapper.get_url(base + "moving")
            sup = self.wrapper.get_url(base + "away")
            FileManager.save_json_file({
                "moving": Extractor.units_overview(mv) if mv else {},
                "support": Extractor.units_overview(sup) if sup else {},
                "when": int(time.time()),
            }, "cache/troops_moving.json")
        except Exception as exc:
            # Non-critical: the dashboard falls back to lumped "away". Still log
            # at debug so a persistent parse/markup regression is visible.
            logging.getLogger("twb").debug("update_troop_movements failed: %s", exc)

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
                "TWB: village %s was lost (conquered/nobled). Removing it from the bot." % vid)
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
        """
        active_h = [int(hour) for hour in config["bot"]["active_hours"].split("-")]
        start, end = active_h[0], active_h[1]
        get_h = time.localtime().tm_hour
        if start <= end:
            # Same-day window; the end hour is inclusive ("6-23" is active
            # 06:00-23:59, matching how a user reads "6 to 23").
            return start <= get_h <= end
        # Overnight window that wraps past midnight (e.g. "22-6"): active from
        # the start hour through the end hour inclusive.
        return get_h >= start or get_h <= end

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
        while self.should_run:
            time.sleep(random.randint(low, high))
            if not self.should_run:
                break
            # Mirror the main loop's activity window so we don't poll all night
            # on an otherwise dormant account.
            if not self.is_active_hours(config=config) and not config["bot"].get(
                    "inactive_still_active", False
            ):
                continue
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
                next_send = attack_scheduler.next_send_ts()
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

    def run(self):
        """
        Run the bot
        TODO: make less messy
        """
        Notification.send("TWB is starting up")
        config = self.config()
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

        self.wrapper.start()
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
        while self.should_run:
            # Heartbeat: proof the main loop is still turning, independent of the
            # incoming-attack poller and scheduler threads, which run on their own
            # non-blocking wrappers and keep logging even when this loop is stuck
            # (e.g. waiting out a captcha in WebWrapper._await_captcha_clear).
            # OverviewBuilder uses staleness here as a generic "bot stalled" signal.
            FileManager.save_json_file_atomic(
                {"ts": int(time.time()), "runs": self.runs}, "cache/heartbeat.json")
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

                village_number = 1
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

                    village.run(config=config)

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
                )
                print(
                    "Dead for %.2f minutes (next run at: %s)"
                    % (sleep / 60, dt_next.time())
                )
                sys.stdout.flush()
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
            Notification.send("TWB crashed: %s" % str(e))
            # Write the full traceback to the rotating log file (cache/twb.log)
            # as well as stderr, so the crash survives the tmux pane / restart.
            logging.getLogger("twb").exception("I crashed :( %s", str(e))
            traceback.print_exc()

    Notification.send("TWB has crashed 3 times, exiting")


def resolve_world_dir():
    """Honour `--world <name>`: point config.json + cache/ at worlds/<name>/.

    Lets several bot instances share one source tree and dashboard while keeping
    fully separate config, session and cache per world. Must run before any
    config/cache access. With no --world the data dir stays the project root, so
    single-world setups are completely unchanged. Returns the world name or None.
    """
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
    os.makedirs(os.path.join(data_dir, "cache"), exist_ok=True)
    FileManager.set_data_dir(data_dir)
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
