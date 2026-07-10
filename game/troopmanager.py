"""
Anything that has to do with the recruiting of troops
"""
import logging
import math
import random
import time

from core.extractors import Extractor
from core.filemanager import FileManager
from game.resources import ResourceManager

# TribalWars scavenging loot ratio per option (I-IV): the resources a run
# returns equal the squad's total carry capacity times this factor.
SCAVENGE_LOOT_FACTOR = {1: 0.10, 2: 0.25, 3: 0.50, 4: 0.75}

# Scavenge run duration in seconds:
#   ((carry^2 * 100 * loot_factor^2) ^ 0.45 + 1800) * world_speed ^ -0.55
# Community formula, verified against a live nl99 (speed 2.0) option-IV run:
# 39,160 carry predicted 16h10m, observed 16h09m.
SCAVENGE_DURATION_EXPONENT = 0.45
SCAVENGE_DURATION_BASE_SECONDS = 1800
SCAVENGE_DURATION_SPEED_EXPONENT = -0.55


def scavenge_max_carry(option, max_seconds, world_speed=1.0):
    """Largest squad carry capacity whose scavenge run on `option` returns
    within max_seconds (inverse of the duration formula). 0 when not even a
    minimal squad would make it back in time."""
    factor = SCAVENGE_LOOT_FACTOR.get(int(option), 0)
    if not factor or not max_seconds or max_seconds <= 0:
        return 0
    speed_factor = float(world_speed or 1.0) ** SCAVENGE_DURATION_SPEED_EXPONENT
    inner = max_seconds / speed_factor - SCAVENGE_DURATION_BASE_SECONDS
    if inner <= 0:
        return 0
    carry_squared = inner ** (1 / SCAVENGE_DURATION_EXPONENT) / (100 * factor * factor)
    return int(math.sqrt(carry_squared))


def scavenge_loot(option, carry_max):
    """Expected loot for one dispatched squad = carry capacity * option ratio."""
    try:
        return int(int(carry_max) * SCAVENGE_LOOT_FACTOR.get(int(option), 0))
    except (TypeError, ValueError):
        return 0


def log_scavenge_run(village_id, loot):
    """Record one scavenging run (a full gather cycle for a village).

    A run sends troops to every available option at once, so we count the cycle
    as a single run and sum its options' expected loot. The completed-scavenging
    reports carry no haul, so this dispatch-time figure is the loot source for
    the dashboard's 24h / total numbers.
    """
    loot = int(loot or 0)
    if loot <= 0:
        return
    log = FileManager.load_json_file("cache/scavenge_log.json") or {}
    now = int(time.time())
    runs = log.get("runs", []) or []
    # Backfill the all-time run counter from existing runs the first time.
    log["total_runs"] = int(log.get("total_runs", len(runs))) + 1
    log["total_loot"] = int(log.get("total_loot", 0)) + loot
    runs.append({"when": now, "loot": loot, "village": str(village_id)})
    # Keep ~25h of runs for the 24h window; the all-time figures live in the
    # total_* counters so the list stays small.
    cutoff = now - 90000
    log["runs"] = [r for r in runs if r.get("when", 0) >= cutoff]
    FileManager.save_json_file(log, "cache/scavenge_log.json")


class TroopManager:
    """
    Troopmanager class
    """
    can_recruit = True
    can_attack = True
    can_dodge = False
    can_farm = True
    can_gather = True
    can_fix_queue = True
    randomize_unit_queue = True

    queue = []
    troops = {}

    total_troops = {}

    _research_wait = 0

    wrapper = None
    village_id = None
    recruit_data = {}
    game_data = {}
    logger = None
    max_batch_size = 50
    wait_for = {}

    _waits = {}

    wanted = {"barracks": {}}

    # Maps troops to the building they are created from
    unit_building = {
        "spear": "barracks",
        "sword": "barracks",
        "axe": "barracks",
        "archer": "barracks",
        "spy": "stable",
        "light": "stable",
        "marcher": "stable",
        "heavy": "stable",
        "ram": "garage",
        "catapult": "garage",
    }

    wanted_levels = {}

    last_gather = 0

    resman = None
    template = None

    def __init__(self, wrapper=None, village_id=None):
        """
        Create the troop manager
        """
        self.wrapper = wrapper
        self.village_id = village_id
        self.wait_for[village_id] = {"barracks": 0, "stable": 0, "garage": 0}
        if not self.resman:
            self.resman = ResourceManager(
                wrapper=self.wrapper, village_id=self.village_id
            )

    def update_totals(self):
        """
        Updates the total amount of recruited units
        """
        main_data = self.wrapper.get_action(
            action="overview", village_id=self.village_id
        )
        self.game_data = Extractor.game_state(main_data)

        if self.resman:
            if "research" in self.resman.requested:
                # new run, remove request
                self.resman.requested["research"] = {}

        if not self.logger:
            village_name = self.game_data["village"]["name"]
            self.logger = logging.getLogger(f"Recruitment: {village_name}")
        self.troops = {}

        get_all = (
                f"game.php?village={self.village_id}&screen=place&mode=units&display=units"
        )
        result_all = self.wrapper.get_url(get_all)

        for u in Extractor.units_in_village(result_all):
            k, v = u
            self.troops[k] = v

        self.logger.debug("Units in village: %s", str(self.troops))

        if not self.can_recruit:
            return

        self.total_troops = {}
        for u in Extractor.units_in_total(result_all):
            k, v = u
            if k in self.total_troops:
                self.total_troops[k] = self.total_troops[k] + int(v)
            else:
                self.total_troops[k] = int(v)
        self.logger.debug("Village units total: %s", str(self.total_troops))

    def start_update(self, building="barracks", disabled_units=[]):
        """
        Starts the unit update for a building
        """
        if self.wait_for[self.village_id][building] > time.time():
            human_ts = self.readable_ts(self.wait_for[self.village_id][building])
            self.logger.info(
                "%s still busy for %s",
                building, human_ts
            )
            return False

        run_selection = list(self.wanted[building].keys())
        if self.randomize_unit_queue:
            random.shuffle(run_selection)

        for wanted in run_selection:
            # Ignore disabled units
            if wanted in disabled_units:
                continue

            if wanted not in self.total_troops:
                if self.recruit(
                        wanted, self.wanted[building][wanted], building=building
                ):
                    return True
                continue

            if self.wanted[building][wanted] > self.total_troops[wanted]:
                if self.recruit(
                        wanted,
                        self.wanted[building][wanted] - self.total_troops[wanted],
                        building=building,
                ):
                    return True

        self.logger.info("Recruitment:%s up-to-date", building)
        return False

    def get_min_possible(self, entry):
        """
        Calculates which units are needed the most
        To get some balance of the total amount
        """
        return min(
            [
                math.floor(self.game_data["village"]["wood"] / entry["wood"]),
                math.floor(self.game_data["village"]["stone"] / entry["stone"]),
                math.floor(self.game_data["village"]["iron"] / entry["iron"]),
                math.floor(
                    (
                            self.game_data["village"]["pop_max"]
                            - self.game_data["village"]["pop"]
                    )
                    / entry["pop"]
                ),
            ]
        )

    def get_template_action(self, levels):
        """
        Read data from templates and determine the troops based op building progression
        """
        last = None
        wanted_upgrades = {}
        for x in self.template:
            if x["building"] not in levels:
                return last

            if x["level"] > levels[x["building"]]:
                return last

            last = x
            if "upgrades" in x:
                for unit in x["upgrades"]:
                    if (
                            unit not in wanted_upgrades
                            or x["upgrades"][unit] > wanted_upgrades[unit]
                    ):
                        wanted_upgrades[unit] = x["upgrades"][unit]

            self.wanted_levels = wanted_upgrades
        return last

    def research_time(self, time_str):
        """
        Calculates unit research time
        """
        parts = [int(x) for x in time_str.split(":")]
        return parts[2] + (parts[1] * 60) + (parts[0] * 60 * 60)

    def attempt_upgrade(self):
        """
        Attempts to upgrade or research a (new) unit type
        """
        self.logger.debug("Managing Upgrades")
        if self._research_wait > time.time():
            self.logger.debug(
                "Smith still busy for %d seconds", int(self._research_wait - time.time())
            )
            return
        unit_levels = self.wanted_levels
        if not unit_levels:
            self.logger.debug("Not upgrading because nothing is requested")
            return
        result = self.wrapper.get_action(village_id=self.village_id, action="smith")
        smith_data = Extractor.smith_data(result)
        if not smith_data:
            self.logger.debug("Error reading smith data")
            return False
        for unit_type in unit_levels:
            if not smith_data or unit_type not in smith_data["available"]:
                self.logger.warning(
                    "Unit %s does not appear to be available or smith not built yet", unit_type
                )
                continue
            wanted_level = unit_levels[unit_type]
            current_level = int(smith_data["available"][unit_type]["level"])
            data = smith_data["available"][unit_type]

            if (
                    current_level < wanted_level
                    and "can_research" in data
                    and data["can_research"]
            ):
                if "research_error" in data and data["research_error"]:
                    self.logger.debug(
                        "Skipping research of %s because of research error", unit_type
                    )
                    # Add needed resources to res manager?
                    r = True
                    if data["wood"] > self.game_data["village"]["wood"]:
                        req = data["wood"] - self.game_data["village"]["wood"]
                        self.resman.request(source="research", resource="wood", amount=req)
                        r = False
                    if data["stone"] > self.game_data["village"]["stone"]:
                        req = data["stone"] - self.game_data["village"]["stone"]
                        self.resman.request(source="research", resource="stone", amount=req)
                        r = False
                    if data["iron"] > self.game_data["village"]["iron"]:
                        req = data["iron"] - self.game_data["village"]["iron"]
                        self.resman.request(source="research", resource="iron", amount=req)
                        r = False
                    if not r:
                        self.logger.debug("Research needs resources")
                    continue
                if "error_buildings" in data and data["error_buildings"]:
                    self.logger.debug(
                        "Skipping research of %s because of building error", unit_type
                    )
                    continue

                attempt = self.attempt_research(unit_type, smith_data=smith_data)
                if attempt:
                    self.logger.info(
                        "Started smith upgrade of %s %d -> %d",
                        unit_type, current_level, current_level + 1
                    )
                    self.wrapper.reporter.report(
                        self.village_id,
                        "TWB_UPGRADE",
                        "Started smith upgrade of %s %d -> %d"
                        % (unit_type, current_level, current_level + 1),
                    )
                    return True
        return False

    def attempt_research(self, unit_type, smith_data=None):
        if not smith_data:
            result = self.wrapper.get_action(village_id=self.village_id, action="smith")
            smith_data = Extractor.smith_data(result)
        if not smith_data or unit_type not in smith_data["available"]:
            self.logger.warning(
                "Unit %s does not appear to be available or smith not built yet", unit_type
            )
            return
        data = smith_data["available"][unit_type]
        if "can_research" in data and data["can_research"]:
            if "research_error" in data and data["research_error"]:
                self.logger.debug(
                    "Ignoring research of %s because of resource error %s", unit_type, str(data["research_error"])
                )
                # Add needed resources to res manager?
                r = True
                if data["wood"] > self.game_data["village"]["wood"]:
                    req = data["wood"] - self.game_data["village"]["wood"]
                    self.resman.request(source="research", resource="wood", amount=req)
                    r = False
                if data["stone"] > self.game_data["village"]["stone"]:
                    req = data["stone"] - self.game_data["village"]["stone"]
                    self.resman.request(source="research", resource="stone", amount=req)
                    r = False
                if data["iron"] > self.game_data["village"]["iron"]:
                    req = data["iron"] - self.game_data["village"]["iron"]
                    self.resman.request(source="research", resource="iron", amount=req)
                    r = False
                if not r:
                    self.logger.debug("Research needs resources")
                return False
            if "error_buildings" in data and data["error_buildings"]:
                self.logger.debug(
                    "Ignoring research of %s because of building error %s", unit_type, str(data["error_buildings"])
                )
                return False
            if (
                    "level" in data
                    and "level_highest" in data
                    and data["level_highest"] != 0
                    and data["level"] == data["level_highest"]
            ):
                return False
            res = self.wrapper.get_api_action(
                village_id=self.village_id,
                action="research",
                params={"screen": "smith"},
                data={
                    "tech_id": unit_type,
                    "source": self.village_id,
                    "h": self.wrapper.last_h,
                },
            )
            if res:
                if "research_time" in data:
                    self._research_wait = time.time() + self.research_time(
                        data["research_time"]
                    )
                self.logger.info("Started research of %s", unit_type)
                # self.resman.update(res["game_data"])
                return True
        self.logger.info("Research of %s not yet possible", unit_type)

    @staticmethod
    def _unlock_started(res):
        """True when a start_unlock API response indicates the unlock began.
        The game answers a successful unlock with the option's new state; a
        rejected one (e.g. not enough resources) carries an error."""
        if not res or not isinstance(res, dict):
            return False
        if res.get("error") or res.get("errors") or res.get("error_code"):
            return False
        return True

    def unlock_scavenge(self, max_option=4):
        """Auto-unlock scavenging options up to ``max_option`` (1..4), lowest
        level first. Only one option can be unlocking at a time, so this starts
        at most one unlock per call.

        Returns a status dict:
          ``pending``    - a wanted option is still locked and was not started
                           this call (already unlocking, or unaffordable).
          ``started``    - the option id we just began unlocking, else None.
          ``affordable`` - False when a wanted option was skipped because the
                           server rejected the unlock (insufficient resources).
        Callers use ``pending`` + ``not affordable`` to decide whether to hold
        building so resources can accumulate for the unlock."""
        status = {"pending": False, "started": None, "affordable": True}
        if max_option < 1:
            return status

        url = f"game.php?village={self.village_id}&screen=place&mode=scavenge"
        result = self.wrapper.get_url(url=url)
        village_data = Extractor.village_data(result)
        options = (village_data or {}).get("options") or {}
        if not options:
            return status

        # The game only allows one option to be unlocking at a time. If any is
        # mid-unlock, there is nothing to start this cycle.
        for opt, o in options.items():
            if o and o.get("unlock_time"):
                status["pending"] = True
                return status

        for opt in sorted(options.keys(), key=lambda x: int(x)):
            if int(opt) > max_option:
                break
            o = options[opt] or {}
            if not o.get("is_locked"):
                continue
            # Lowest locked option within target: try to unlock just this one.
            payload = {
                "village_id": self.village_id,
                "option_id": str(int(opt)),
                "h": self.wrapper.last_h,
            }
            res = self.wrapper.get_api_action(
                action="start_unlock",
                params={"screen": "scavenge_api"},
                data=payload,
                village_id=self.village_id,
            )
            if self._unlock_started(res):
                self.logger.info("Started unlocking scavenge option %s", opt)
                status["started"] = int(opt)
            else:
                self.logger.info(
                    "Scavenge option %s not unlocked yet (insufficient resources?)", opt
                )
                status["pending"] = True
                status["affordable"] = False
            return status
        return status

    def gather(self, selection=1, disabled_units=[], advanced_gather=True, consolidate=0):
        """
        Used for the gather resources functionality where it uses two options:
        - Basic: all troops gather on the selected gather level
        - Advanced: troops are split

        consolidate (night mode): seconds left until the night window ends.
        When > 0, override the split and send troops into a single run on the
        highest unlocked level <= selection, capped so the run is back home
        when the window ends and normal spread runs take over. Troops that
        don't fit the cap go out on the next-lower level in a later cycle.
        """
        if not self.can_gather:
            return False
        url = f"game.php?village={self.village_id}&screen=place&mode=scavenge"
        result = self.wrapper.get_url(url=url)
        village_data = Extractor.village_data(result)

        # Snapshot each scavenge option's state (locked / running / idle, plus the
        # active squad's expected loot + return time) for the dashboard.
        self.scavenge_state = []
        for opt in sorted((village_data.get('options') or {}).keys(), key=lambda x: int(x)):
            o = village_data['options'][opt] or {}
            squad = o.get('scavenging_squad')
            carry = (squad or {}).get('carry_max')
            try:
                loot = int(int(carry) * SCAVENGE_LOOT_FACTOR.get(int(opt), 0)) if carry else 0
            except (TypeError, ValueError):
                loot = 0
            self.scavenge_state.append({
                'option': int(opt),
                'locked': bool(o.get('is_locked')),
                'running': squad is not None,
                'return_at': (squad or {}).get('return_time') or (squad or {}).get('finished_at'),
                'loot': loot,
            })

        sleep = 0
        available_selection = 0
        cycle_haul = 0  # summed expected loot across this run's options

        self.troops = {}

        get_all = f"game.php?village={self.village_id}&screen=place&mode=units&display=units"
        result_all = self.wrapper.get_url(get_all)

        for u in Extractor.units_in_village(result_all):
            k, v = u
            self.troops[k] = v

        troops = dict(self.troops)

        haul_dict = [
            "spear:25",
            "sword:15",
            "heavy:50",
            "axe:10",
            "light:80"
        ]
        if "archer" in self.total_troops:
            haul_dict.extend(["archer:10", "marcher:50"])

        # ADVANCED GATHER: Goes from gather_selection to 1, trying the same time (approximately) for every gather. Active hours exclude LC and Axes, at night everything is used for gather (except Paladin)

        if advanced_gather and not consolidate:
            selection_map = [15, 21, 24,
                             26]  # Divider in order to split the total carrying capacity of the troops into pieces that can fit into pretty much the same time frame

            batch_multiplier = [15, 6, 3,
                                2]  # Multiplier for equal distribution of troops. Time(gather1) = Time(gather2) if gather2 = 2.5 * gather1

            troops = {key: int(value) for key, value in troops.items()}
            total_carry = 0
            for item in haul_dict:
                item, carry = item.split(":")
                if item == "knight":
                    continue
                if item in disabled_units:
                    continue
                if item in troops and int(troops[item]) > 0:
                    total_carry += int(carry) * int(troops[item])
                else:
                    pass
            gather_batch = math.floor(total_carry / selection_map[selection - 1])

            for option in list(reversed(sorted(village_data['options'].keys())))[4 - selection:]:
                self.logger.debug(
                    f"Option: {option} Locked? {village_data['options'][option]['is_locked']} Is underway? {village_data['options'][option]['scavenging_squad'] != None}")
                if int(option) <= selection and not village_data['options'][option]['is_locked'] and not \
                village_data['options'][option]['scavenging_squad'] != None:
                    available_selection = int(option)
                    self.logger.info(f"Gather operation {available_selection} is ready to start.")

                    payload = {
                        "squad_requests[0][village_id]": self.village_id,
                        "squad_requests[0][option_id]": str(available_selection),
                        "squad_requests[0][use_premium]": "false",
                    }

                    curr_haul = gather_batch * batch_multiplier[available_selection - 1]
                    temp_haul = curr_haul

                    self.logger.debug(
                        f"Current Haul: {curr_haul} = Gather Batch ({gather_batch}) * Batch Multiplier {available_selection} ({batch_multiplier[available_selection - 1]})")

                    for item in haul_dict:
                        item, carry = item.split(":")
                        if item == "knight":
                            continue
                        if item in disabled_units:
                            continue

                        if item in troops and int(troops[item]) > 0:
                            troops_int = int(troops[item])
                            troops_selected = 0
                            for troop in range(troops_int):
                                if (temp_haul - int(carry) < 0):
                                    break
                                else:
                                    troops_selected += 1
                                    temp_haul -= int(carry)
                            troops_int -= troops_selected
                            troops[item] = str(troops_int)
                            payload["squad_requests[0][candidate_squad][unit_counts][%s]" % item] = str(troops_selected)
                        else:
                            payload["squad_requests[0][candidate_squad][unit_counts][%s]" % item] = "0"
                    payload["squad_requests[0][candidate_squad][carry_max]"] = str(curr_haul)
                    payload["h"] = self.wrapper.last_h
                    self.wrapper.get_api_action(
                        action="send_squads",
                        params={"screen": "scavenge_api"},
                        data=payload,
                        village_id=self.village_id,
                    )
                    sleep += random.randint(1, 5)
                    time.sleep(sleep)
                    self.last_gather = int(time.time())
                    cycle_haul += scavenge_loot(available_selection, curr_haul)
                    self.logger.info(f"Using troops for gather operation: {available_selection}")
                else:
                    # Gathering already exists or locked, try next lower option
                    continue

        else:
            for option in reversed(sorted(village_data['options'].keys())):
                self.logger.debug(
                    f"Option: {option} Locked? {village_data['options'][option]['is_locked']} Is underway? {village_data['options'][option]['scavenging_squad'] != None}")
                if int(option) <= selection and not village_data['options'][option]['is_locked'] and not \
                village_data['options'][option]['scavenging_squad'] != None:
                    available_selection = int(option)
                    self.logger.info(f"Gather operation {available_selection} is ready to start.")
                    selection = available_selection

                    carry_budget = None
                    if consolidate:
                        world_cfg = FileManager.load_json_file("cache/world/config.json") or {}
                        world_speed = float(world_cfg.get("speed", 1) or 1)
                        carry_budget = scavenge_max_carry(
                            available_selection, consolidate, world_speed
                        )
                        if carry_budget <= 0:
                            self.logger.info(
                                "Night consolidation: no time left for a run on option %s, skipping",
                                available_selection,
                            )
                            break
                        self.logger.info(
                            "Night consolidation: capping option %s run at %d carry to return within %dm",
                            available_selection, carry_budget, consolidate // 60,
                        )

                    payload = {
                        "squad_requests[0][village_id]": self.village_id,
                        "squad_requests[0][option_id]": str(available_selection),
                        "squad_requests[0][use_premium]": "false",
                    }
                    total_carry = 0
                    for item in haul_dict:
                        item, carry = item.split(":")
                        if item == "knight":
                            continue
                        if item in disabled_units:
                            continue
                        if item in troops and int(troops[item]) > 0:
                            count = int(troops[item])
                            if carry_budget is not None:
                                count = min(count, max(0, (carry_budget - total_carry) // int(carry)))
                            payload[
                                "squad_requests[0][candidate_squad][unit_counts][%s]" % item
                                ] = str(count)
                            total_carry += int(carry) * count
                        else:
                            payload[
                                "squad_requests[0][candidate_squad][unit_counts][%s]" % item
                                ] = "0"
                    payload["squad_requests[0][candidate_squad][carry_max]"] = str(total_carry)
                    if total_carry > 0:
                        payload["h"] = self.wrapper.last_h
                        self.wrapper.get_api_action(
                            action="send_squads",
                            params={"screen": "scavenge_api"},
                            data=payload,
                            village_id=self.village_id,
                        )
                        self.last_gather = int(time.time())
                        cycle_haul += scavenge_loot(selection, total_carry)
                        self.logger.info(f"Using troops for gather operation: {selection}")
                        if consolidate:
                            # Night mode: everything went into this single
                            # highest-level run; don't feed the lower levels.
                            break
                else:
                    # Gathering already exists or locked, try next lower option
                    continue
        # One run per gather cycle, with the cycle's options summed.
        log_scavenge_run(self.village_id, cycle_haul)
        self.logger.info("All gather operations are underway.")
        return True

    def cancel(self, building, id):
        """
        Cancel a troop recruiting action
        """
        self.wrapper.get_api_action(
            action="cancel",
            params={"screen": building},
            data={"id": id},
            village_id=self.village_id,
        )

    def recruit(self, unit_type, amount=10, wait_for=False, building="barracks"):
        """
        Recruit x amount of x from a certain building
        """
        data = self.wrapper.get_action(action=building, village_id=self.village_id)

        existing = Extractor.active_recruit_queue(data)
        if existing:
            if not self.can_fix_queue:
                # A pre-existing queue is the expected case when we're not
                # allowed to touch it - log quietly instead of warning.
                self.logger.debug(
                    "Building Village %s %s recruitment queue already populated, leaving as-is"
                    % (self.village_id, building)
                )
                return True
            self.logger.warning(
                "Building Village %s %s recruitment queue out-of-sync, clearing it"
                % (self.village_id, building)
            )
            for entry in existing:
                self.cancel(building=building, id=entry)
                self.logger.info(
                    "Canceled recruit item %s on building %s" % (entry, building)
                )
            return self.recruit(unit_type, amount, wait_for, building)

        self.recruit_data = Extractor.recruit_data(data)
        self.game_data = Extractor.game_state(data)
        self.logger.info("Attempting recruitment of %d %s" % (amount, unit_type))

        if amount > self.max_batch_size:
            amount = self.max_batch_size

        if unit_type not in self.recruit_data:
            self.logger.warning(
                "Recruitment of %d %s failed because it is not researched"
                % (amount, unit_type)
            )
            self.attempt_research(unit_type)
            return False

        resources = self.recruit_data[unit_type]
        if not resources:
            self.logger.warning(
                "Recruitment of %d %s failed because invalid identifier"
                % (amount, unit_type)
            )
            return False
        if not resources["requirements_met"]:
            self.logger.warning(
                "Recruitment of %d %s failed because it is not researched"
                % (amount, unit_type)
            )
            self.attempt_research(unit_type)
            return False

        get_min = self.get_min_possible(resources)
        if get_min == 0:
            self.logger.info(
                "Recruitment of %d %s failed because of not enough resources"
                % (amount, unit_type)
            )
            self.reserve_resources(resources, amount, get_min, unit_type)
            return False

        needed_reserve = False
        if get_min < amount:
            if wait_for:
                self.logger.warning(
                    "Recruitment of %d %s failed because of not enough resources"
                    % (amount, unit_type)
                )
                self.reserve_resources(resources, amount, get_min, unit_type)
                needed_reserve = True
                return False
            if get_min > 0:
                self.logger.info(
                    "Recruitment of %d %s was set to %d because of resources"
                    % (amount, unit_type, get_min)
                )
                self.reserve_resources(resources, amount, get_min, unit_type)
                amount = get_min
                needed_reserve = True

        if not needed_reserve:
            # No need to reserve resources anymore!
            if f"recruitment_{unit_type}" in self.resman.requested:
                self.resman.requested.pop(f"recruitment_{unit_type}", None)

        result = self.wrapper.get_api_action(
            village_id=self.village_id,
            action="train",
            params={"screen": building, "mode": "train"},
            data={"units[%s]" % unit_type: str(amount)},
        )
        if result and "game_data" in result:
            self.resman.update(result["game_data"])
            self.wait_for[self.village_id][building] = int(time.time()) + (
                    amount * int(resources["build_time"])
            )
            # self.troops[unit_type] = str((int(self.troops[unit_type]) if unit_type in self.troops else 0) + amount)
            self.logger.info(
                "Recruitment of %d %s started (%s idle till %d)",
                    amount,
                    unit_type,
                    building,
                    self.wait_for[self.village_id][building],
            )
            self.wrapper.reporter.report(
                self.village_id,
                "TWB_RECRUIT",
                "Recruitment of %d %s started (%s idle till %d)"
                % (
                    amount,
                    unit_type,
                    building,
                    self.wait_for[self.village_id][building],
                ),
            )
            return True
        return False

    def reserve_resources(self, resources, wanted_times, has_times, unit_type):
        """
        Reserve resources for a certain recruiting action
        """
        # Resources per unit, batch wanted, batch already recruiting
        create_amount = wanted_times - has_times
        self.logger.debug(f"Requesting resources to recruit %d of %s", create_amount, unit_type)
        for res in ["wood", "stone", "iron"]:
            req = resources[res] * (wanted_times - has_times)
            self.resman.request(source=f"recruitment_{unit_type}", resource=res, amount=req)

    def readable_ts(self, seconds):
        """
        Human readable timestamp
        """
        seconds -= time.time()
        seconds = seconds % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60

        return "%d:%02d:%02d" % (hour, minutes, seconds)
