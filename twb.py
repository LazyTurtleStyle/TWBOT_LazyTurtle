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
from manager import VillageManager
from pages.overview import OverviewPage
from core.exceptions import UnsupportedPythonVersion
from core.extractors import Extractor

coloredlogs.install(
    level=logging.DEBUG if "-q" not in sys.argv else logging.INFO,
    fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

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

        # Prefer the already-parsed villages_data (BS4-based, position-stable).
        # Fall back to the regex extractor if the table parse returned nothing.
        if overview_page.villages_data:
            self.found_villages = list(overview_page.villages_data.keys())
        else:
            self.found_villages = Extractor.village_ids_from_overview(overview_page.result_get.text)
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

        return overview_page, config

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
        get_h = time.localtime().tm_hour
        return get_h in range(active_h[0], active_h[1])

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
        while self.should_run:
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

                VillageManager.farm_manager(verbose=True)
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
            t.wrapper.reporter.report(0, "TWB_EXCEPTION", str(e))
            print("I crashed :(   %s" % str(e))
            Notification.send("TWB crashed: %s" % str(e))
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
