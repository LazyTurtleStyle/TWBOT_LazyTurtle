import json
import logging
import math
import random
import re
import time

from core.extractors import Extractor
from core.filemanager import FileManager


class DefenceManager:
    wrapper = None
    village_id = None
    units = None
    map = None

    under_attack = False
    auto_evacuate = False
    attacks = []

    # list of village_id, attack_state
    my_other_villages = {}
    allow_support_send = True
    allow_support_recv = True

    defensive_units = ["spear", "sword", "archer", "marcher", "spy"]

    # Units that are pointless (or too precious) to leave home during an
    # attack: the whole off plus the noble. Defensive units stay and fight.
    hide_units = ["snob", "axe", "light", "marcher", "ram", "catapult"]

    flags = {}

    runs = 0
    logger = None
    manage_flags_enabled = True
    # Only combine 3-of-a-kind flags into a higher level when explicitly enabled,
    # so the bot never consumes the user's flags without being asked.
    auto_upgrade_flags = False
    # Which flag type to keep assigned on this village (TribalWars flag type id:
    # 1 resource, 2 recruitment, 3 attack, 4 defense, 5 luck, 6 population,
    # 7 coin cost, 8 haul). 0 = manage upgrades only, never assign a flag.
    flag_type = 1
    support_factor = 0.25
    support_max_villages = 2

    # flag_index, flag_level
    current_flag = []

    _can_change_flag = False

    _sf_logged = False

    supported = []

    def __init__(self, village_id=None, wrapper=None):
        self.village_id = village_id
        self.wrapper = wrapper
        self.logger = logging.getLogger("Defence Manager")

    def support_other(self, requesting_village):

        if self.under_attack or not self.allow_support_send:
            return False
        if not self.units:
            return False
        send_support = {}
        for u in self.defensive_units:
            if u in self.units.troops and int(self.units.troops[u]) > 0:
                send_support[u] = int(int(self.units.troops[u]) * self.support_factor)

        self.logger.info(
            "Sending requested support to village %s: %s", requesting_village, str(send_support)
        )
        return self.support(requesting_village, troops=send_support)

    @staticmethod
    def detect_incoming(main):
        """
        Detect whether the village is under attack.

        Primary signal: the account-wide incoming-attack count exposed in the
        page's game data (player.incomings). This is robust against asset
        renames (e.g. command/attack.png -> .webp). For single-village accounts
        this maps directly to "this village is under attack".

        Fallback: the legacy command/attack icon string, in case the game data
        is unavailable for some reason.
        """
        game_data = Extractor.game_state(main)
        if game_data:
            incomings = game_data.get("player", {}).get("incomings")
            if incomings is not None:
                try:
                    return int(incomings) > 0
                except (TypeError, ValueError):
                    pass
        return "command/attack.png" in main or "command/attack.webp" in main

    def update(self, main, with_defence=False):
        ok = True
        self.manage_flags()
        self.runs += 1
        # Keep the village's configured flag assigned regardless of attack state
        # (no attack-time override - a manual defence action will live elsewhere).
        self.flag_logic(self.flag_type)
        if self.detect_incoming(main):
            self.under_attack = True
            ok = False
            # Evacuation is triggered from Village.run *after* the troop counts
            # are refreshed, so the send uses live numbers (and works on the
            # first cycle too) - not from here.
        else:
            if not with_defence:
                self.under_attack = False
                return False
            self.under_attack = False
            index = 0

            for vil in self.my_other_villages:
                if vil != self.village_id:
                    continue
                if len(self.supported) >= self.support_max_villages:
                    self.logger.debug("Already supported 2 villages, ignoring")
                    break
                if (
                        not self.under_attack
                        and self.my_other_villages[vil]
                        and self.allow_support_send
                ):
                    if vil in self.supported:
                        continue
                    if index >= 2:
                        continue
                    if self.support_other(vil):
                        self.supported.append(vil)
                    ok = False
                index += 1
        if ok:
            self.logger.info("Area OK for village %s, nice and quiet", self.village_id)
            # All is well

    @staticmethod
    def villages_with_incomings():
        """Own village ids that currently have an incoming attack, according
        to the incoming poller's cache (arrivals still in the future)."""
        targeted = set()
        now = time.time()
        try:
            files = FileManager.list_directory(
                "cache/incomings", ends_with=".json")
        except Exception:
            return targeted
        for name in files:
            entry = FileManager.load_json_file(f"cache/incomings/{name}") or {}
            arrival = entry.get("arrival")
            if entry.get("target_id") and (arrival is None or arrival > now):
                targeted.add(str(entry["target_id"]))
        return targeted

    @staticmethod
    def _managed_locations():
        """{village_id: [x, y]} for every managed village in the cache."""
        locations = {}
        try:
            files = FileManager.list_directory(
                "cache/managed", ends_with=".json")
        except Exception:
            return locations
        for name in files:
            entry = FileManager.load_json_file(f"cache/managed/{name}") or {}
            loc = (entry.get("public") or {}).get("location")
            if isinstance(loc, list) and len(loc) == 2:
                locations[name[:-len(".json")]] = loc
        return locations

    def evacuate(self):
        """Dodge the fragile units out to the nearest own village that has no
        incoming attack itself. They arrive as support and stay there until
        pulled back manually (there is no auto-return)."""
        if not self.units:
            return False
        to_hide = {}
        for u in self.hide_units:
            if u in self.units.troops and int(self.units.troops[u]) > 0:
                to_hide[u] = int(self.units.troops[u])
        if not to_hide:
            return False
        # under_attack is account-wide (player.incomings); the incoming
        # poller's cache says which village the attacks actually target. Only
        # skip when the cache positively points at other villages - an empty
        # cache (poller off/behind) must not stop a dodge.
        targeted = self.villages_with_incomings()
        if targeted and str(self.village_id) not in targeted:
            self.logger.debug(
                "Incoming attacks target other villages - village %s stays put",
                self.village_id
            )
            return False
        locations = self._managed_locations()
        here = locations.get(str(self.village_id))
        candidates = [
            vid for vid in locations
            if vid != str(self.village_id) and vid not in targeted
        ]
        if not candidates:
            self.logger.warning(
                "Village %s wants to evacuate %s but no safe own village is "
                "known - troops stay home", self.village_id, str(to_hide)
            )
            return False

        # Nearest safe village first, so the troops are back in range soonest.
        def _distance(vid):
            if not here:
                return 9999.0
            loc = locations[vid]
            return math.hypot(loc[0] - here[0], loc[1] - here[1])

        for vid in sorted(candidates, key=_distance):
            self.logger.info(
                "Evacuating troops from village %s to %s: %s",
                self.village_id, vid, str(to_hide)
            )
            if self.support(vid, troops=to_hide, coords=locations[vid]):
                return True
            self.logger.warning(
                "Evacuation send to village %s failed, trying the next one", vid
            )
        return False

    def flag_logic(self, set_flag):
        if not self.manage_flags_enabled:
            return
        if not set_flag or set_flag <= 0:
            return  # flag_type 0 -> never assign a flag (upgrades only)

        highest_flag_possible = self.get_highest_flag_possible(flag_id=set_flag)
        if not highest_flag_possible:
            return

        if (
                not self.current_flag
                or self.current_flag[0] is not set_flag
                or highest_flag_possible and highest_flag_possible > self.current_flag[1]
        ):
            if not self._can_change_flag:
                if not self._sf_logged:
                    self.logger.info(
                        "Unable to set new flag on village %s because of cool down", self.village_id
                    )
                    self._sf_logged = True
                return
            self._sf_logged = False
            self.flag_set(
                set_flag, level=self.get_highest_flag_possible(flag_id=set_flag)
            )
            self.logger.info(
                "Setting flag %d level %d for village %s",
                set_flag, self.get_highest_flag_possible(flag_id=set_flag), self.village_id
            )

    def flag_upgrade(self, flag, level):
        return self.wrapper.get_api_action(
            self.village_id,
            action="upgrade_flag",
            params={"screen": "flags", "h": self.wrapper.last_h},
            data={"flag_type": flag, "from_level": level},
        )

    def flag_set(self, flag, level):
        return self.wrapper.get_api_action(
            self.village_id,
            action="assign_flag",
            params={"screen": "flags", "h": self.wrapper.last_h},
            data={
                "flag_type": str(flag),
                "level": str(level),
                "village_id": self.village_id,
            },
        )

    def get_highest_flag_possible(self, flag_id=1):
        if flag_id not in self.flags:
            return None
        return self.flags[flag_id]

    def manage_flags(self):
        if not self.manage_flags_enabled:
            return
        # Randomize flag runs
        if self.runs != 0 and self.runs % random.randint(3, 8) != 0:
            return
        self.logger.info("Managing flags")

        url = f"game.php?village={self.village_id}&screen=flags"
        result = self.wrapper.get_url(url=url)

        self._can_change_flag = '<span class="timer cooldown">' not in result.text

        get_flag_data = re.search(r"FlagsScreen\.setFlagCounts\((.+?)\);", result.text)
        if not get_flag_data:
            self.logger.warning("Error reading flag data")
            return
        get_current_flag = re.search(
            r'(?s)<div id="current_flag".+?/(\d+)_(\d+)\.png.+?<p>(.+?)</p>.+?</div>',
            result.text,
        )
        if get_current_flag:
            if '<div id="current_flag" style="margin-top: 10px; display: none">' in result.text:
                self.logger.warning(
                    "No flag was identified on village, setting default one"
                )
                self.current_flag = None
            else:
                cflag = [int(get_current_flag.group(1)), int(get_current_flag.group(2))]
                if cflag != self.current_flag:
                    self.current_flag = cflag
                    self.logger.info(
                        "Current village flag: %s", get_current_flag.group(3).strip()
                    )
        upgraded = 0
        raw_flags = json.loads(get_flag_data.group(1))
        self.flags = {}
        for flag_type in raw_flags:
            for level in raw_flags[flag_type]:
                for amount in raw_flags[flag_type][level]:
                    if self.auto_upgrade_flags and int(amount) >= 3:
                        self.flag_upgrade(flag=flag_type, level=level)
                        self.logger.info("Upgraded flag %s", flag_type)
                        upgraded += 1
                    if int(amount) > 0:
                        if int(flag_type) not in self.flags or self.flags[
                            int(flag_type)
                        ] < int(level):
                            self.flags[int(flag_type)] = int(level)
        if upgraded:
            return self.manage_flags()

    def support(self, vid, troops=None, coords=None):
        url = f"game.php?village={self.village_id}&screen=place&target={vid}"
        pre_support = self.wrapper.get_url(url)
        pre_data = {}
        for u in Extractor.attack_form(pre_support):
            k, v = u
            pre_data[k] = v
        if troops:
            pre_data.update(troops)
        else:
            pre_data.update(self.units.troops)

        if not coords:
            if not self.map or vid not in self.map.map_pos:
                return False
            coords = self.map.map_pos[vid]
        x, y = coords
        post_data = {"x": x, "y": y, "target_type": "coord", "support": "Ondersteunen"}
        pre_data.update(post_data)

        confirm_url = f"game.php?village={self.village_id}&screen=place&try=confirm"
        conf = self.wrapper.post_url(url=confirm_url, data=pre_data)
        if '<div class="error_box">' in conf.text:
            return False
        duration = Extractor.attack_duration(conf)
        self.logger.info(
            "[Support] %s -> %s duration %f.1 h",
            self.village_id, vid, duration / 3600
        )

        confirm_data = {}
        for u in Extractor.attack_form(conf):
            k, v = u
            if k == "attack":
                continue
            confirm_data[k] = v
        new_data = {"h": self.wrapper.last_h}
        confirm_data.update(new_data)
        # Match the farm/scheduler path: the extractor drops the empty cb value
        # and mistakes it for x, so re-add the literal "x" key (not the coord
        # value) with the x coordinate.
        if "x" not in confirm_data:
            confirm_data["x"] = x
        result = self.wrapper.get_api_action(
            village_id=self.village_id,
            action="popup_command",
            params={"screen": "place"},
            data=confirm_data,
        )

        return result
