import logging

from core.extractors import Extractor


class DefenceManager:
    wrapper = None
    village_id = None
    units = None
    map = None

    under_attack = False
    attacks = []

    # list of village_id, attack_state
    my_other_villages = {}
    allow_support_send = True
    allow_support_recv = True

    defensive_units = ["spear", "sword", "archer", "marcher", "spy"]

    # Units that are pointless (or too precious) to leave home during an
    # attack: the whole off plus the noble. Defensive units stay and fight.

    runs = 0
    logger = None
    support_factor = 0.25
    support_max_villages = 2

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
        self.runs += 1
        # Flags are not handled here. They are an account-wide inventory - one
        # flag sits on exactly one village - so deciding them per village made
        # every village fight the others over the same few flags. game/flags.py
        # owns them now, in one pass that counts the pool.
        if self.detect_incoming(main):
            self.under_attack = True
            ok = False
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
