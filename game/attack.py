"""
Attack manager
Sounds dangerous but it just sends farms
"""

from core.extractors import Extractor
import logging
import random
import time
from datetime import datetime
from datetime import timedelta

from core.filemanager import FileManager
from game.simulator import Simulator


class AttackManager:
    """
    Attackmanager class
    """
    map = None
    village_id = None
    troopmanager = None
    wrapper = None
    targets = {}
    logger = logging.getLogger("Attacks")
    max_farms = 15
    extra_farm = []
    repman = None
    target_high_points = False
    farm_radius = 50
    farm_minpoints = 0
    farm_maxpoints = 1000
    ignored = []

    # Farm Assistant template ids (configured in-game, see config.example.json comment)
    template_id_scout = None
    template_id_minimal = None
    # Troops the in-game minimal (B) template actually sends, e.g. {"light": 5, "spy": 1}.
    # Mirror this from the in-game template - there is no API to read it back.
    template_minimal_troops = {}
    # Skip the farm and wait for next cycle if the target's last known wall level is
    # expected to cost us this many troops or more (average case, real combat luck
    # varies per attack) - applies to both the minimal (B) and report (C) farms
    minimal_loss_tolerance = 0.5
    # Carry capacity of a single light cavalry, used to estimate how many we'd need to
    # send to fully loot a fresh scout report (C, farm_from_report) - we don't get to
    # choose the troops for that action, the in-game Farm Assistant sizes them to the
    # loot, so this lets us estimate the minimum it would use. Mirrors
    # Simulator.pool["light"]["load"]; only valid for farm armies that are pure light cav
    light_cavalry_load = 80
    sim = Simulator()
    # Per-target A/B/C farm icon state scraped from the am_farm overview page, refreshed
    # once per run() - tells us if the game itself already disabled an icon (not enough
    # troops, or the wall makes it a guaranteed loss) and, for C, the exact troop forecast
    farm_icons = {}
    _warned_other_troops = set()
    # Report this fresh or fresher -> trust its exact loot numbers (C, farm_from_report)
    report_freshness_hours = 6
    # Report older than this (or no report at all) -> re-scout (A) instead of trusting it
    report_max_age_hours = 24
    # How long to wait before retrying a target we just sent a scout to (report still in transit)
    scout_wait = 900

    # Only run the farming logic once per this random window (seconds), independent of how
    # often the rest of the village loop ticks - keeps the send cadence less bot-like
    farm_run_interval_min = 1500
    farm_run_interval_max = 2700
    _last_farm_run = 0
    _next_farm_run_delay = 0

    forced_peace_time = None

    # Don't mess with these they are in the config file
    farm_high_prio_wait = 1200
    farm_default_wait = 3600
    farm_low_prio_wait = 7200

    def __init__(self, wrapper=None, village_id=None, troopmanager=None, map=None):
        """
        Create the attack manager
        """
        self.wrapper = wrapper
        self.village_id = village_id
        self.troopmanager = troopmanager
        self.map = map

    def run(self):
        """
        Run the farming logic
        """
        now = int(time.time())
        if now < self._last_farm_run + self._next_farm_run_delay:
            return False
        if not self.troopmanager.can_attack:
            # Disable farming is disabled in config
            return False
        if not self.template_id_scout or not self.template_id_minimal:
            self.logger.warning(
                "Farm Assistant template ids are not configured (farms.template_id_scout / "
                "farms.template_id_minimal), cannot farm"
            )
            return False
        self.get_targets()
        self.farm_icons = self.fetch_farm_icons()
        # Limits the amount of villages that are farmed from the current village
        for target in self.targets[0: self.max_farms]:
            self.send_farm(target)
        self._last_farm_run = now
        self._next_farm_run_delay = random.randint(
            self.farm_run_interval_min, self.farm_run_interval_max
        )

    def due_for_attack(self, vid):
        """
        Checks the cooldown timer for a village, based on the kind of the last send
        """
        cache_entry = AttackCache.get_cache(vid)
        if not cache_entry:
            return True
        wait = {
            "report": self.farm_high_prio_wait,
            "minimal": self.farm_default_wait,
        }.get(cache_entry.get("kind"), self.scout_wait)

        if self.repman:
            res_left, res = self.repman.has_resources_left(vid)
            total_loot = sum(int(v) for v in res.values()) if res else 0
            if res_left and total_loot > 100:
                self.logger.debug(f"Draining farm of resources! Sending attack to get {res}.")
                wait = int(self.farm_high_prio_wait / 2)

        return cache_entry["last_attack"] + wait <= int(time.time())

    def fetch_farm_icons(self):
        """
        Reads the live A/B/C farm icon state from the am_farm overview page - this is the
        same data the in-game page uses to grey out an icon (not enough troops, or the
        wall makes the attack a guaranteed loss), and for C it includes the exact troop
        forecast the game calculated, which we otherwise can't see ahead of sending.
        """
        res = self.wrapper.get_url(f"game.php?village={self.village_id}&screen=am_farm")
        if not res:
            return {}
        return Extractor.farm_assistant_icons(res)

    def send_farm(self, target):
        """
        Decide and send a single Farm Assistant action (A/B/C) for a target
        """
        target, _ = target
        vid = target["id"]
        if not self.due_for_attack(vid):
            return

        icons = self.farm_icons.get(vid, {})

        # C only ever uses a scout report - that's what the in-game "from report" action reads
        report_id, extra, age = (None, None, None)
        if self.repman:
            report_id, extra, age = self.repman.latest_for(vid, report_type="scout")

        if report_id is None or age is None or age > self.report_max_age_hours * 3600:
            # No report, or too old to trust at all -> re-scout
            self._send(vid, kind="scout")
            return

        status = self.repman.safe_to_engage(vid) if self.repman else -1
        if status == 0:
            self.logger.debug(
                "%s will be ignored for farm because last report shows defenders", vid
            )
            return
        if status != 1:
            self._send(vid, kind="scout")
            return

        if age <= self.report_freshness_hours * 3600:
            # Safe and fresh enough to trust exact loot -> let Farm Assistant size the
            # attack to the loot (C), but only if those troops wouldn't die on the wall
            c_icon = icons.get("c")
            if c_icon is not None:
                if c_icon["disabled"]:
                    self.logger.debug(
                        "%s: C farm icon is disabled in-game (not enough troops, or the "
                        "wall would make it a guaranteed loss), skipping", vid
                    )
                    return
                forecast = c_icon["forecast"] or {}
                self.warn_if_other_troops(vid, forecast, "C (live forecast)")
                wall = (extra.get("buildings") or {}).get("wall")
                if wall is not None and self._troops_too_risky(vid, forecast, wall, "report"):
                    return
            elif self.report_farm_too_risky(vid, extra):
                # Icon wasn't on the page (fetch failed, or target not listed) -> fall
                # back to our own loot-based estimate
                return
            self._send(vid, kind="report", report_id=report_id)
        else:
            # Safe, but the report is too old to trust the exact loot numbers -> minimal LC
            b_icon = icons.get("b")
            if b_icon is not None and b_icon["disabled"]:
                self.logger.debug("%s: B farm icon is disabled in-game, skipping", vid)
                return
            if self.minimal_farm_too_risky(vid, extra):
                return
            self._send(vid, kind="minimal")

    def warn_if_other_troops(self, vid, troops, source):
        """
        Both minimal_farm_too_risky() and the C wall-risk check assume a farm army of
        pure light cavalry. Warn loudly (and only once per vid) if a troop set we can
        see - either the configured B template, or the live C forecast - includes any
        other fighting unit, since the wall-risk math would then be wrong.
        """
        other_units = {
            unit: count
            for unit, count in troops.items()
            if unit != "light" and unit in self.sim.pool and count
        }
        if other_units and vid not in self._warned_other_troops:
            self._warned_other_troops.add(vid)
            self.logger.error(
                "%s: %s includes non-light-cavalry troops %s - wall-risk checks assume "
                "pure light cavalry and will be wrong here",
                vid, source, other_units,
            )

    def check_farm_unit_composition(self):
        """
        Called once at config load. The B template (template_minimal_troops) is the only
        farm composition we can see ahead of time without contacting the game - the C
        composition is checked live, per target, in send_farm() instead.
        """
        self.warn_if_other_troops(
            "config", self.template_minimal_troops, "B template (template_minimal_troops)"
        )

    def minimal_farm_too_risky(self, vid, extra):
        """
        The scout report only proves the target had no defenders at scout time, not
        that it still has none now. A high wall level punishes new defenders that
        spawned since then far more than a low one, so skip the minimal (B) farm
        when the expected loss for our configured B troops is too high.
        """
        if not self.template_minimal_troops:
            return False
        wall = (extra.get("buildings") or {}).get("wall")
        if wall is None:
            return False
        return self._troops_too_risky(vid, self.template_minimal_troops, wall, "minimal")

    def report_farm_too_risky(self, vid, extra):
        """
        Fallback used only when the live C farm icon wasn't found on the am_farm page.
        farm_from_report (C) lets the in-game Farm Assistant pick the troops, sized to
        carry the reported loot - we don't choose them directly. Since our farm army is
        pure light cavalry, estimate the minimum it would need to carry that loot and
        check that against the wall. Sending more troops than this estimate only lowers
        the loss fraction further (wall defense doesn't scale with attacker size), so
        this minimum is the worst case.
        """
        wall = (extra.get("buildings") or {}).get("wall")
        if wall is None:
            return False
        resources = extra.get("resources") or {}
        total_loot = sum(int(v) for v in resources.values())
        if total_loot <= 0:
            return False
        light_needed = -(-total_loot // self.light_cavalry_load)  # ceil division
        return self._troops_too_risky(vid, {"light": light_needed}, wall, "report")

    def _troops_too_risky(self, vid, troops, wall, label):
        losses = self.sim.simulate_against_wall(troops, wall)
        total_loss = sum(losses.values())
        if total_loss >= self.minimal_loss_tolerance:
            self.logger.debug(
                "%s: skipping %s farm, wall level %s would cost ~%.1f troops of %s",
                vid, label, wall, total_loss, troops
            )
            return True
        return False

    def _send(self, vid, kind, report_id=None):
        """
        Sends the actual Farm Assistant action and records the cooldown cache entry.
        A failure here (e.g. Farm Assistant refuses a report-based send because the
        loot isn't worth the risk of losing LC against the wall) is treated as
        transient: we just skip and let the next farm cycle retry, never a permanent
        block on the target.
        """
        if kind == "scout":
            result = self.farm_template(vid, self.template_id_scout)
        elif kind == "minimal":
            result = self.farm_template(vid, self.template_id_minimal)
        else:
            result = self.farm_from_report(report_id)

        if result and not (isinstance(result, dict) and result.get("error")):
            self.logger.info(
                "Farm Assistant [%s] %s -> %s", kind, self.village_id, vid
            )
            self.wrapper.reporter.report(
                self.village_id,
                "TWB_FARM",
                "Farm Assistant [%s] %s -> %s" % (kind, self.village_id, vid),
            )
            self.attacked(vid, kind)
        else:
            self.logger.debug(
                "Skipping target %s this cycle, Farm Assistant send was refused: %s", vid, result
            )

    def get_targets(self):
        """
        Gets all possible farming targets based on distance
        """
        output = []
        my_village = (
            self.map.villages[self.village_id]
            if self.village_id in self.map.villages
            else None
        )
        for vid in self.map.villages:
            village = self.map.villages[vid]
            if village["owner"] != "0" and vid not in self.extra_farm:
                if vid not in self.ignored:
                    self.logger.debug(
                        "Ignoring village %s because player owned, add to additional_farms to auto attack", vid
                    )
                    self.ignored.append(vid)
                continue
            if my_village and "points" in my_village and "points" in village:
                if village["points"] >= self.farm_maxpoints:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because points %d exceeds limit %d",
                            vid, village["points"], self.farm_maxpoints
                        )
                        self.ignored.append(vid)
                    continue
                if village["points"] <= self.farm_minpoints:
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because points %d below limit %d",
                            vid, village["points"], self.farm_minpoints
                        )
                        self.ignored.append(vid)
                    continue
                if (
                        village["points"] >= my_village["points"]
                        and not self.target_high_points
                ):
                    if vid not in self.ignored:
                        self.logger.debug(
                            "Ignoring village %s because of higher points %d -> %d",
                            vid, my_village["points"], village["points"]
                        )
                        self.ignored.append(vid)
                    continue
            if village["owner"] != "0":
                get_h = time.localtime().tm_hour
                if get_h in range(0, 8) or get_h == 23:
                    self.logger.debug(
                        "Village %s will be ignored because it is player owned and attack between 23h-8h", vid
                    )
                    continue
            distance = self.map.get_dist(village["location"])
            if distance > self.farm_radius:
                if vid not in self.ignored:
                    self.logger.debug(
                        "Village %s will be ignored because it is too far away: distance is %f, max is %d",
                        vid, distance, self.farm_radius
                    )
                    self.ignored.append(vid)
                continue
            if vid in self.ignored:
                self.logger.debug("Removed %s from farm ignore list", vid)
                self.ignored.remove(vid)

            output.append([village, distance])
        self.logger.info(
            "Farm targets: %d Ignored targets: %d", len(output), len(self.ignored)
        )
        self.targets = sorted(output, key=lambda x: x[1])

    def attacked(self, vid, kind):
        """
        The farm assistant action was sent and this is a callback to record cooldown state
        """
        cache_entry = {
            "kind": kind,
            "last_attack": int(time.time()),
        }
        AttackCache.set_cache(vid, cache_entry)

    def has_troops_available(self, troops):
        for t in troops:
            if (
                    t not in self.troopmanager.troops
                    or int(self.troopmanager.troops[t]) < troops[t]
            ):
                return False
        return True

    def attack(self, vid, troops=None):
        """
        Send a TW attack
        """
        url = f"game.php?village={self.village_id}&screen=place&target={vid}"
        pre_attack = self.wrapper.get_url(url)
        pre_data = {}
        for u in Extractor.attack_form(pre_attack):
            k, v = u
            pre_data[k] = v
        if troops:
            pre_data.update(troops)
        else:
            pre_data.update(self.troopmanager.troops)

        if vid not in self.map.map_pos:
            return False

        x, y = self.map.map_pos[vid]
        post_data = {"x": x, "y": y, "target_type": "coord", "attack": "Aanvallen"}
        pre_data.update(post_data)

        confirm_url = f"game.php?village={self.village_id}&screen=place&try=confirm"
        conf = self.wrapper.post_url(url=confirm_url, data=pre_data)
        if '<div class="error_box">' in conf.text:
            return False
        duration = Extractor.attack_duration(conf)
        if self.forced_peace_time:
            now = datetime.now()
            if now + timedelta(seconds=duration) > self.forced_peace_time:
                self.logger.info("Attack would arrive after the forced peace timer, not sending attack!")
                return "forced_peace"

        self.logger.info(
            "[Attack] %s -> %s duration %f.1 h", self.village_id, vid, duration / 3600
        )

        confirm_data = {}
        for u in Extractor.attack_form(conf):
            k, v = u
            if k == "support":
                continue
            confirm_data[k] = v
        new_data = {"building": "main", "h": self.wrapper.last_h}
        confirm_data.update(new_data)
        # The extractor doesn't like the empty cb value, and mistakes its value for x. So I add it here.
        if "x" not in confirm_data:
            confirm_data["x"] = x

        result = self.wrapper.get_api_action(
            village_id=self.village_id,
            action="popup_command",
            params={"screen": "place"},
            data=confirm_data,
        )

        return result

    def farm_template(self, vid, template_id):
        """
        Sends one of the in-game Farm Assistant templates (A or B) to a target village
        """
        return self.wrapper.get_api_action(
            village_id=self.village_id,
            action="farm",
            params={"screen": "am_farm", "mode": "farm", "json": "1"},
            data={
                "target": vid,
                "template_id": template_id,
                "source": self.village_id,
            },
        )

    def farm_from_report(self, report_id):
        """
        Asks the in-game Farm Assistant to send exactly enough troops to loot
        the resources shown in the given scout report (the "C" action)
        """
        return self.wrapper.get_api_action(
            village_id=self.village_id,
            action="farm_from_report",
            params={"screen": "am_farm", "mode": "farm", "json": "1"},
            data={"report_id": report_id},
        )


class AttackCache:
    @staticmethod
    def get_cache(village_id):
        return FileManager.load_json_file(f"cache/attacks/{village_id}.json")

    @staticmethod
    def set_cache(village_id, entry):
        return FileManager.save_json_file(entry, f"cache/attacks/{village_id}.json")

    @staticmethod
    def cache_grab():
        output = {}

        for existing in FileManager.list_directory("cache/attacks", ends_with=".json"):
            output[existing.replace(".json", "")] = FileManager.load_json_file(f"cache/attacks/{existing}")
        return output
