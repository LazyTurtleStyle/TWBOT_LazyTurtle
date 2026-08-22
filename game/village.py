import json
import logging
import time
from codecs import decode
from datetime import datetime, timedelta

from core.server_clock import ServerClock
from core.extractors import Extractor
from core.filemanager import FileManager
from core.templates import TemplateManager
from core.twstats import TwStats
from game.attack import AttackManager
from game.barbshaper import BarbShaper
from game.balancer import ResourceBalancer
from game.buildingmanager import BuildingManager
from game.defence_manager import DefenceManager
from game.incomings import load_groups
from game.map import Map
from game.reports import ReportManager
from game.resources import ResourceManager
from game.snobber import SnobManager
from game.troopmanager import TroopManager
from core.exceptions import *


class Village:
    village_id = None
    builder = None
    balancer = None
    units = None
    wrapper = None
    resources = {}
    game_data = {}
    logger = None
    force_troops = False
    area = None
    snobman = None
    attack = None
    barb_shaper = None
    resman = None
    def_man = None
    rep_man = None
    config = None
    forced_peace_today = False
    village_set_name = None
    last_attack = None
    build_config = None
    current_unit_entry = None
    forced_peace = False
    forced_peace_today_start = None
    disabled_units = []
    # How many complete-quest/claim-reward rounds one cycle may do. Quest work
    # used to re-enter run() per completed quest with no bound at all; see
    # run_quest_actions().
    MAX_QUEST_ROUNDS = 5

    twp = TwStats()

    def __init__(self, village_id=None, wrapper=None):
        self.village_id = village_id
        self.wrapper = wrapper
        # {unit: count} this village must keep home this cycle, set by the run
        # loop before run(). Currently the escort of an armed noble job, which
        # is only sent after every village has run (see twb.py).
        self.troop_reserve = {}

    def get_config(self, section, parameter, default=None):
        # A missing key just means an older config predates the parameter;
        # every caller supplies a default, so this is debug noise, not a warning.
        if section not in self.config:
            self.logger.debug("Configuration section %s does not exist", section)
            return default
        if parameter not in self.config[section]:
            self.logger.debug(
                "Configuration parameter %s:%s does not exist", section, parameter
            )
            return default
        return self.config[section][parameter]

    def get_village_config(self, village_id, parameter, default=None):
        if village_id not in self.config["villages"]:
            return default
        vdata = self.config["villages"][village_id]
        if parameter not in vdata:
            self.logger.debug(
                "Village %s configuration parameter %s does not exist",
                village_id, parameter
            )
            return default
        return vdata[parameter]

    def village_init(self):
        """
        Init the village entry and send first request
        """
        if not self.village_id:
            data = self.wrapper.get_url("game.php?screen=overview&intro")
            if data:
                self.game_data = Extractor.game_state(data)
            if self.game_data:
                self.village_id = str(self.game_data["village"]["id"])
                self.logger = logging.getLogger(
                    "Village %s" % self.game_data["village"]["name"]
                )
                self.logger.info("Read game state for village")
        else:
            data = self.wrapper.get_url(
                f"game.php?village={self.village_id}&screen=overview"
            )
            if data:
                self.game_data = Extractor.game_state(data)
            if self.game_data:
                self.logger = logging.getLogger(
                    "Village %s" % self.game_data["village"]["name"]
                )
                self.logger.info("Read game state for village")
                self.wrapper.reporter.report(
                    self.village_id,
                    "TWB_START",
                    "Starting run for village: %s" % self.game_data["village"]["name"],
                )
        if not self.game_data:
            # Page didn't contain a valid game state (e.g. a login / redirect
            # page after a re-auth). Don't crash here; let run() handle it.
            if not self.logger:
                self.logger = logging.getLogger("Village %s" % self.village_id)
            self.logger.error(
                "Could not read game state for village %s (got a non-game page)",
                self.village_id,
            )
            return data
        if (
                self.village_set_name
                and self.game_data["village"]["name"] != self.village_set_name
        ):
            self.logger.name = f"Village {self.village_set_name}"
        return data

    def set_world_config(self):
        """
        Sets basic world options
        """
        self.disabled_units = []
        if not self.get_config(
                section="world", parameter="archers_enabled", default=True
        ):
            self.disabled_units.extend(["archer", "marcher"])

        if not self.get_config(
                section="world", parameter="building_destruction_enabled", default=True
        ):
            self.disabled_units.extend(["ram", "catapult"])

        if self.get_config(
                section="server", parameter="server_on_twstats", default=False
        ):
            self.twp.run(world=self.get_config(section="server", parameter="server"))

    def update_pre_run(self):
        """
        Manage defence, resources and reports
        """
        if not self.resman:
            self.resman = ResourceManager(
                wrapper=self.wrapper, village_id=self.village_id
            )

        self.resman.update(self.game_data)
        self.wrapper.reporter.report(
            self.village_id, "TWB_PRE_RESOURCE", str(self.resman.actual)
        )

        if not self.rep_man:
            self.rep_man = ReportManager(
                wrapper=self.wrapper, village_id=self.village_id
            )
        self.rep_man.read(full_run=False)

        if not self.def_man:
            self.def_man = DefenceManager(
                wrapper=self.wrapper, village_id=self.village_id
            )
        # The map is only built later (farming), so keep re-wiring it until it
        # exists instead of copying the None once at creation time.
        if not self.def_man.map and self.area:
            self.def_man.map = self.area

        if not self.def_man.units and self.units:
            self.def_man.units = self.units

    def setup_defence_manager(self, data):
        """
        Set-up the defence manager
        """
        # Flag management is now its own feature, independent of world.flags_enabled
        # (which is just a "this world has flags" capability marker). The flags
        # section drives whether we manage flags at all and whether we auto-upgrade;
        # the flag to keep assigned is chosen per village.
        self.def_man.manage_flags_enabled = self.get_config(
            section="flags", parameter="manage", default=False
        )
        self.def_man.auto_upgrade_flags = self.get_config(
            section="flags", parameter="auto_upgrade", default=False
        )
        self.def_man.flag_type = self.get_village_config(
            self.village_id, parameter="flag_type", default=1
        )
        self.def_man.support_factor = self.get_village_config(
            self.village_id, "support_others_factor", default=0.25
        )
        self.def_man.support_max_villages = self.get_village_config(
            self.village_id, "support_others_max_villages", default=2
        )

        self.def_man.allow_support_send = self.get_village_config(
            self.village_id, parameter="support_others", default=False
        )
        self.def_man.allow_support_recv = self.get_village_config(
            self.village_id, parameter="request_support_on_attack", default=False
        )
        self.def_man.auto_evacuate = self.get_village_config(
            self.village_id, parameter="evacuate_fragile_units_on_attack", default=False
        )
        self.def_man.update(
            data.text,
            with_defence=self.get_config(
                section="units", parameter="manage_defence", default=False
            ),
        )
        if self.def_man.under_attack and not self.last_attack:
            self.logger.warning("Village under attack!")
            self.wrapper.reporter.report(
                self.village_id,
                "TWB_ATTACK",
                "Village: %s under attack" % self.game_data["village"]["name"],
            )
        self.last_attack = self.def_man.under_attack

    def run_quest_actions(self, config):
        """
        Complete finished quests and collect their rewards.

        This used to ``return self.run(config=config)`` on every completed quest
        so the rest of the cycle would see the reward resources, and nothing
        bounded that recursion. A quest the game refuses to close still reports
        itself as finished, and the reward claim below - the thing that actually
        makes such a quest go away - sat *after* the re-run, so it was never
        reached. Seen live 2026-08-07: 485 nested runs of village 001 over 4h08,
        no other village got a turn, and the process finally died on a failed
        request inside that flood of traffic.

        So: a bounded number of complete-then-claim rounds, never the same quest
        twice in one cycle, and the caller's cycle simply continues afterwards -
        get_quest_rewards() credits the reward to resman, so the builder and
        recruiter still see the resources without re-running anything.
        """
        if not self.get_config(
                section="world", parameter="quests_enabled", default=False
        ):
            return
        attempted = set()
        for _ in range(self.MAX_QUEST_ROUNDS):
            quest = self.get_quests(skip=attempted)
            if quest:
                attempted.add(quest)
                self.wrapper.reporter.report(
                    self.village_id, "TWB_QUEST", "Completed quest"
                )
            got_rewards = self.get_quest_rewards()
            if got_rewards:
                self.wrapper.reporter.report(
                    self.village_id, "TWB_QUEST", "Collected quest reward(s)"
                )
            if not quest and not got_rewards:
                return
        self.logger.warning(
            "Still finding quest work after %d rounds, continuing the cycle anyway",
            self.MAX_QUEST_ROUNDS,
        )

    def units_get_template(self):
        """
        Fetches the unit template
        """
        if not self.units:
            self.units = TroopManager(wrapper=self.wrapper, village_id=self.village_id)
            self.units.resman = self.resman
        self.units.max_batch_size = self.get_config(
            section="units", parameter="batch_size", default=25
        )

        # set village templates
        unit_config = self.get_village_config(
            self.village_id, parameter="units", default=None
        )
        if unit_config is False:
            # Per-village off-switch: recruit nothing here, whatever the
            # global units.recruit master switch says.
            self.logger.debug(
                "Recruiting is disabled for village %s", self.village_id)
            self.units.template = []
            self.units.wanted = {}
            return
        if not unit_config:
            self.logger.warning(
                "Village %d does not have 'units' config override!", self.village_id
            )
            unit_config = self.get_config(
                section="units", parameter="default", default="basic"
            )
        try:
            self.units.template = TemplateManager.get_template(
                category="troops", template=unit_config, output_json=True
            )
        except Exception as e:
            self.logger.error(
                "Looks like the unit template file %s is either missing or corrupted", unit_config
            )
            raise InvalidUnitTemplateException

    def run_builder(self):
        """
        Run building construction actions
        """
        if not self.builder:
            self.builder = BuildingManager(
                wrapper=self.wrapper, village_id=self.village_id
            )
            self.builder.resman = self.resman
            # manage buildings (has to always run because recruit check depends on building levels)
        self.build_config = self.get_village_config(
            self.village_id, parameter="building", default=None
        )
        # Per-village off-switch (building: false). Still fall through to
        # start_update with build=False: that call is what reads the building
        # levels, which the recruit templates need even when nothing is built.
        build_disabled = self.build_config is False
        if build_disabled:
            self.logger.debug("Builder is disabled for village %s", self.village_id)
        elif not self.build_config:
            self.logger.warning(
                "Village %d does not have 'building' config override!", self.village_id
            )
            self.build_config = self.get_config(
                section="building", parameter="default", default="purple_predator"
            )
        if not build_disabled:
            new_queue = TemplateManager.get_template(
                category="builder", template=self.build_config
            )
            if not self.builder.raw_template or self.builder.raw_template != new_queue:
                self.builder.queue = new_queue
                self.builder.raw_template = new_queue
                if not self.get_config(
                        section="world", parameter="knight_enabled", default=False
                ):
                    self.builder.queue = [
                        x for x in self.builder.queue if "statue" not in x
                    ]
        self.builder.max_lookahead = self.get_config(
            section="building", parameter="max_lookahead", default=2
        )
        self.builder.max_queue_len = self.get_config(
            section="building", parameter="max_queued_items", default=2
        )
        # Population-priority farm: a per-village value >= 0 overrides the
        # global building setting; -1 (or absent) inherits it. 0 = off.
        farm_pop = self.get_config(
            section="building", parameter="farm_priority_pop_pct", default=0
        )
        v_override = self.config.get("villages", {}).get(self.village_id, {}).get(
            "farm_priority_pop_pct", -1
        )
        if isinstance(v_override, (int, float)) and v_override >= 0:
            farm_pop = v_override
        try:
            self.builder.farm_priority_pop_pct = int(farm_pop or 0)
        except (TypeError, ValueError):
            self.builder.farm_priority_pop_pct = 0
        self.builder.start_update(
            build=not build_disabled and self.get_config(
                section="building", parameter="manage_buildings", default=True
            ),
            set_village_name=self.village_set_name,
        )

    def run_snob_recruit(self):
        """
        Uses the snob to mint coins, store resources and recruit snobs
        """
        if (
                self.get_village_config(self.village_id, parameter="snobs", default=None)
                and self.builder.levels["snob"] > 0
        ):
            if not self.snobman:
                self.snobman = SnobManager(
                    wrapper=self.wrapper, village_id=self.village_id
                )
                self.snobman.troop_manager = self.units
                self.snobman.resman = self.resman
            self.snobman.wanted = self.get_village_config(
                self.village_id, parameter="snobs", default=0
            )
            self.snobman.building_level = self.builder.get_level("snob")
            self.snobman.run()

    def check_forced_peace(self):
        """
        Checks if farming is disabled for the current time
        """
        # Set timeslots in order to prevent farming during events like national holidays
        forced_peace_times = self.get_config(section="farms", parameter="forced_peace_times", default=[])
        self.forced_peace = False
        self.forced_peace_today = False
        self.forced_peace_today_start = None
        for time_pairs in forced_peace_times:
            start_dt = datetime.strptime(time_pairs["start"], "%d.%m.%y %H:%M:%S")
            end_dt = datetime.strptime(time_pairs["end"], "%d.%m.%y %H:%M:%S")
            # The windows are announced by the game in *its* wall clock, so they
            # have to be compared against it: on a host in another timezone,
            # datetime.now() is the same instant but a different reading, and the
            # peace window would start and end hours off.
            now = ServerClock.now()
            if start_dt.date() == now.date():
                self.forced_peace_today = True
                self.forced_peace_today_start = start_dt
            if start_dt < now < end_dt:
                self.logger.debug("Currently in a forced peace time! No attacks will be send.")
                self.forced_peace = True
                break

    def set_unit_wanted_levels(self):
        """
        Fetches wanted units for the current buildings
        """
        self.current_unit_entry = self.units.get_template_action(self.builder.levels)

        if self.current_unit_entry and self.units.wanted != self.current_unit_entry["build"]:
            # update wanted units if template has changed
            self.logger.info(
                "%s as wanted units for current village", str(self.current_unit_entry["build"])
            )
            self.units.wanted = self.current_unit_entry["build"]

        if self.units.wanted_levels != {}:
            # Remove disabled units
            for disabled in self.disabled_units:
                self.units.wanted_levels.pop(disabled, None)
            self.logger.info(
                "%s as wanted upgrades for current village", str(self.units.wanted_levels)
            )

    def run_unit_upgrades(self):
        """
        Uses smith to research or upgrade units
        """
        if (
                self.get_config(section="units", parameter="upgrade", default=False)
                and self.units.wanted_levels != {}
        ):
            self.units.attempt_upgrade()

    def do_recruit(self):
        """
        Recruits new units
        """
        if self.get_config(section="units", parameter="recruit", default=False):
            self.units.can_fix_queue = self.get_config(
                section="units", parameter="remove_manual_queued", default=False
            )
            self.units.randomize_unit_queue = self.get_config(
                section="units", parameter="randomize_unit_queue", default=True
            )
            # prioritize_building: will only recruit when builder has sufficient funds for queue items
            if (
                    self.get_village_config(
                        self.village_id, parameter="prioritize_building", default=False
                    )
                    and not self.resman.can_recruit()
            ):
                self.logger.info(
                    "Not recruiting because builder has insufficient funds"
                )
                for x in list(self.resman.requested.keys()):
                    if "recruitment_" in x:
                        self.resman.requested.pop(f"{x}", None)
            elif (
                    self.get_village_config(
                        self.village_id, parameter="prioritize_snob", default=False
                    )
                    and self.snobman
                    and self.snobman.can_snob
                    and self.snobman.is_incomplete
            ):
                self.logger.info("Not recruiting because snob has insufficient funds")
                for x in list(self.resman.requested.keys()):
                    if "recruitment_" in x:
                        self.resman.requested.pop(f"{x}", None)
            else:
                # do a build run for every
                for building in self.units.wanted:
                    if not self.builder.get_level(building):
                        self.logger.debug(
                            "Recruit of %s will be ignored because building is not (yet) available", building
                        )
                        continue
                    self.units.start_update(building, self.disabled_units)

    def run_balancer(self):
        """Push spare resources to poorer villages (see game/balancer.py).

        Runs after manage_local_resources so resman.requested is already pruned
        of empty entries: anything left there means this village still wants
        resources itself, and a village that needs resources never gives any
        away.
        """
        if not self.get_config(section="balancer", parameter="enabled", default=False):
            return
        if not self.balancer:
            self.balancer = ResourceBalancer(
                wrapper=self.wrapper, village_id=self.village_id
            )
        cfg = self.config.get("balancer", {}) or {}
        self.balancer.enabled = True
        self.balancer.sender_min_points = int(cfg.get("sender_min_points", 4000))
        self.balancer.receiver_max_points = int(cfg.get("receiver_max_points", 1000))
        self.balancer.target_fill_pct = int(cfg.get("target_fill_pct", 90))
        self.balancer.fill_mode = cfg.get("fill_mode", "even")
        self.balancer.target_order = cfg.get("target_order", "nearest")
        self.balancer.send_cooldown = int(cfg.get("send_cooldown_minutes", 60)) * 60
        # Renamed from max_sends_per_run: the cap is per receiver now, so a
        # sender may top up several villages but none gets served twice.
        self.balancer.max_sends_per_receiver = int(
            cfg.get("max_sends_per_receiver", cfg.get("max_sends_per_run", 1)))
        self.balancer.reserve_merchants = int(cfg.get("reserve_merchants", 0))
        self.balancer.sender_keep = int(cfg.get("sender_keep", 0))
        self.balancer.min_send_amount = int(cfg.get("min_send_amount", 250))

        public = self.area.in_cache(self.village_id) if self.area else None
        my_points = int((public or {}).get("points") or 0)
        try:
            self.balancer.run(
                my_points=my_points,
                my_stock=self.resman.actual,
                has_own_needs=bool(self.resman.requested),
            )
        except Exception as exc:
            # Balancing is a convenience: a bad page parse must not take the
            # village run (and with it building/recruiting) down.
            self.logger.warning("Resource balancing failed: %s", exc)

    def manage_local_resources(self):
        to_dell = []
        for x in self.resman.requested:
            if all(res == 0 for res in self.resman.requested[x].values()):
                # remove empty requests!
                to_dell.append(x)

        for x in to_dell:
            self.resman.requested.pop(x)

        self.logger.debug("Current resources: %s", str(self.resman.actual))
        self.logger.debug("Requested resources: %s", str(self.resman.requested))

    def set_farm_options(self):
        """
        Sets various options for farming management
        """
        self.attack.target_high_points = self.get_config(
            section="farms", parameter="attack_higher_points", default=False
        )
        self.attack.farm_minpoints = self.get_config(
            section="farms", parameter="min_points", default=24
        )
        self.attack.farm_maxpoints = self.get_config(
            section="farms", parameter="max_points", default=1080
        )
        self.attack.farm_radius = self.get_config(
            section="farms", parameter="search_radius", default=50
        )
        self.attack.farm_default_wait = self.get_config(
            section="farms", parameter="default_away_time", default=1200
        )
        self.attack.farm_high_prio_wait = self.get_config(
            section="farms", parameter="full_loot_away_time", default=1800
        )
        self.attack.farm_low_prio_wait = self.get_config(
            section="farms", parameter="low_loot_away_time", default=7200
        )
        self.attack.template_id_scout = self.get_config(
            section="farms", parameter="template_id_scout", default=None
        )
        self.attack.template_id_minimal = self.get_config(
            section="farms", parameter="template_id_minimal", default=None
        )
        self.attack.template_minimal_troops = self.get_config(
            section="farms", parameter="template_minimal_troops", default={}
        )
        self.attack.check_farm_unit_composition()
        self.attack.minimal_loss_tolerance = self.get_config(
            section="farms", parameter="minimal_loss_tolerance", default=0.5
        )
        self.attack.report_freshness_hours = self.get_config(
            section="farms", parameter="report_freshness_hours", default=6
        )
        self.attack.report_max_age_hours = self.get_config(
            section="farms", parameter="report_max_age_hours", default=24
        )
        self.attack.max_kind_refusals = self.get_config(
            section="farms", parameter="max_kind_refusals", default=3
        )

    def setup_attack_manager(self):
        """
        Loads the map and wires up the attack manager, the shared setup behind both
        the farm pass and the barb shaper. Returns False when this village should not
        be attacking at all this cycle (forced peace, attacking disabled, no map).
        """
        if self.forced_peace or not self.units.can_attack:
            return False
        if not self.area:
            self.area = Map(wrapper=self.wrapper, village_id=self.village_id)
        self.area.get_map()
        if not self.area.villages:
            return False
        self.logger.info(
            "%d villages from map cache, (your location: %s)",
                len(self.area.villages),
                ":".join([str(x) for x in self.area.my_location])
        )
        if not self.attack:
            self.attack = AttackManager(
                wrapper=self.wrapper,
                village_id=self.village_id,
                troopmanager=self.units,
                map=self.area,
            )
            self.attack.repman = self.rep_man

        if self.forced_peace_today:
            self.logger.info("Forced peace time coming up today!")
            self.attack.forced_peace_time = self.forced_peace_today_start
        self.set_farm_options()
        return True

    def farm_conflicts_with_reserve(self):
        """Units this village must keep home that the farm pass could spend.

        Farm sends are sized by the in-game Farm Assistant, so unlike the
        shaper and scavenging we cannot hand it a reduced troop count - the
        only way to protect a reserved unit is to skip the pass. That is A
        (scouts), the configured B template, and C (light cavalry, what the
        loot-based forecast sends)."""
        if not self.troop_reserve:
            return []
        spends = {"spy", "light"}
        spends.update(self.get_config(
            section="farms", parameter="template_minimal_troops", default={}) or {})
        return sorted(u for u in self.troop_reserve if u in spends)

    def run_farming(self):
        """
        Runs the farming logic. Needs setup_attack_manager() to have returned True.
        """
        conflict = self.farm_conflicts_with_reserve()
        if conflict:
            self.logger.info(
                "Skipping the farm pass this cycle: the Farm Assistant sizes "
                "its own sends and would spend %s, reserved here for an armed "
                "noble job", ", ".join(conflict))
            return
        if (
                self.get_config(section="farms", parameter="farm", default=False)
                and self.get_village_config(
                    self.village_id, parameter="farm_enabled", default=True
                )
                and not self.def_man.under_attack
        ):
            self.attack.extra_farm = self.get_village_config(
                self.village_id, parameter="additional_farms", default=[]
            )
            self.attack.max_farms = self.get_config(
                section="farms", parameter="max_farms", default=25
            )
            self.attack.run()

    def run_barb_shaper(self):
        """
        Sends axe+ram attacks to raze the walls of nearby barbs (alpha).
        Only runs when the axes aren't claimed by scavenging, and always
        keeps rams home while under attack.
        """
        if not self.get_config(
                section="farms", parameter="barb_shaper", default=False
        ):
            return
        if not self.attack or (self.def_man and self.def_man.under_attack):
            return
        if "ram" in self.disabled_units:
            self.logger.debug("Barb shaper: rams are disabled on this world")
            return
        if not self.barb_shaper:
            self.barb_shaper = BarbShaper(
                wrapper=self.wrapper,
                village_id=self.village_id,
                troopmanager=self.units,
                map=self.area,
                repman=self.rep_man,
                attack_manager=self.attack,
            )
        shaper = self.barb_shaper
        shaper.min_wall = self.get_config(
            section="farms", parameter="shaper_min_wall", default=2)
        shaper.loss_tolerance = self.get_config(
            section="farms", parameter="shaper_loss_tolerance", default=1.0)
        shaper.max_sends = self.get_config(
            section="farms", parameter="shaper_max_sends", default=2)
        shaper.ram_reserve = self.get_config(
            section="farms", parameter="shaper_ram_reserve", default=0)
        shaper.report_max_age_hours = self.get_config(
            section="farms", parameter="report_max_age_hours", default=24)
        shaper.share_scavenge_axes = self.get_config(
            section="farms", parameter="shaper_share_axes", default=False)
        shaper.axe_cap = self.get_config(
            section="farms", parameter="shaper_axe_cap", default=0)
        shaper.max_travel_hours = self.get_config(
            section="farms", parameter="shaper_max_travel_hours", default=0)
        gather_on = self.get_village_config(
            self.village_id, parameter="gather_enabled", default=False)
        excluded = list(self.disabled_units) + list(self.get_village_config(
            self.village_id, parameter="gather_exclude_units", default=[]) or [])
        shaper.scavenge_uses_axes = bool(gather_on and "axe" not in excluded)
        shaper.reserved = dict(self.troop_reserve or {})
        shaper.run()

    def _gather_night_consolidate(self):
        """Seconds remaining in the night-consolidation window, or 0 when the
        feature is off or the current hour is outside the window. In the window
        all scavenging troops go into one long run on the highest level instead
        of being split - covering an unattended night. The returned budget caps
        the run's duration so it is back home by gather_night_end, when normal
        spread runs take over. Turn it off (config or the Scavenging
        quick-toggle) if you expect incoming, so troops do shorter runs and
        return more often for defence.

        gather_night_min_hours (default 5): don't start a new consolidation run
        if fewer than this many hours remain until gather_night_end. Prevents
        pointlessly short consolidation runs late in the night."""
        if not self.get_village_config(
            self.village_id, parameter="gather_night_consolidate", default=False
        ):
            return 0
        start = int(self.get_village_config(
            self.village_id, parameter="gather_night_start", default=23))
        end = int(self.get_village_config(
            self.village_id, parameter="gather_night_end", default=7))
        if start == end:
            return 0
        now = datetime.now()
        hour = now.hour
        if start < end:
            in_window = start <= hour < end
        else:
            # Window wraps past midnight (e.g. 23 -> 6).
            in_window = hour >= start or hour < end
        if not in_window:
            return 0
        end_time = now.replace(hour=end, minute=0, second=0, microsecond=0)
        if end_time <= now:
            end_time += timedelta(days=1)
        seconds_left = int((end_time - now).total_seconds())
        # Don't start a new consolidation run if morning is too close.
        min_hours = int(self.get_village_config(
            self.village_id, parameter="gather_night_min_hours", default=5
        ))
        if min_hours > 0 and seconds_left < min_hours * 3600:
            return 0
        return seconds_left

    def _scavenge_target_option(self, hq_level):
        """Highest scavenge option (1..4) this village should have unlocked
        given its headquarters (main building) level. The per-option HQ
        thresholds are configurable; 0 means nothing should be unlocked yet."""
        thresholds = [
            int(self.get_village_config(self.village_id, parameter="scavenge_unlock_hq_1", default=1)),
            int(self.get_village_config(self.village_id, parameter="scavenge_unlock_hq_2", default=5)),
            int(self.get_village_config(self.village_id, parameter="scavenge_unlock_hq_3", default=8)),
            int(self.get_village_config(self.village_id, parameter="scavenge_unlock_hq_4", default=15)),
        ]
        target = 0
        for option, needed in enumerate(thresholds, start=1):
            if hq_level >= needed:
                target = option
        return target

    def do_scavenge_unlock(self):
        """Auto-unlock scavenging options based on headquarters level, before
        the builder runs. Sets builder.hold_for_scavenge so building can be held
        back while a wanted unlock is pending but unaffordable (when the village
        prioritises unlocking over building). No-op on the first cycle, before
        the builder/units exist."""
        if not self.builder or not self.units:
            return
        if not self.get_village_config(
            self.village_id, parameter="scavenge_unlock_enabled", default=False
        ):
            self.builder.hold_for_scavenge = False
            return

        target = self._scavenge_target_option(self.builder.get_level("main"))
        if target < 1:
            self.builder.hold_for_scavenge = False
            return

        # Skip the extra page fetch once everything we want is already unlocked,
        # using last cycle's scavenge snapshot (recorded by the gather step).
        snapshot = getattr(self.units, "scavenge_state", None)
        if snapshot and all(
            not o.get("locked") for o in snapshot if o.get("option", 0) <= target
        ):
            self.builder.hold_for_scavenge = False
            return

        status = self.units.unlock_scavenge(max_option=target)
        prioritise = self.get_village_config(
            self.village_id, parameter="prioritize_scavenge_unlock", default=False
        )
        self.builder.hold_for_scavenge = bool(
            prioritise and status.get("pending") and not status.get("affordable", True)
        )

    # Group scavenging policies, most restrictive first: a village in several
    # policy groups gets the safest one (never > pause_attacked > always).
    GATHER_GROUP_POLICIES = ("never", "pause_attacked", "always")

    def _gather_group_policy(self):
        """This village's scavenging policy from in-game group membership
        (alpha), or None when no policy group contains it.

        farms.gather_group_policies maps an in-game group (name,
        case-insensitive, or id) to a policy:
          never          - troops always stay home, no scavenging at all
                           (front-def group)
          pause_attacked - scavenge normally, stop while an incoming is up,
                           ignoring gather_when_attacked (mobile-def group)
          always         - keep scavenging even while under attack
                           (safe/rim def group)
        Membership comes from the incoming tracker's hourly group cache;
        without cached groups the policies are inert. A group policy is
        authoritative: it beats the per-village gather_when_attacked flag
        and the quick toggle. 'never' also beats gather_enabled.
        """
        policies = self.get_config(
            section="farms", parameter="gather_group_policies", default={}) or {}
        if not policies:
            return None
        groups = load_groups()
        if not groups:
            self.logger.warning(
                "farms.gather_group_policies is set but no in-game groups are "
                "cached yet - is the incoming tracker running?"
            )
            return None
        lookup = {}
        for key, policy in policies.items():
            if policy in self.GATHER_GROUP_POLICIES:
                lookup[str(key).lower()] = policy
        mine = []
        for group in groups:
            if str(self.village_id) not in [str(v) for v in group.get("villages") or []]:
                continue
            for key in (str(group.get("name", "")).lower(), str(group.get("id", ""))):
                if key in lookup:
                    mine.append(lookup[key])
        for policy in self.GATHER_GROUP_POLICIES:
            if policy in mine:
                return policy
        return None

    def do_gather(self):
        """
        Runs gathering if unlocked and active. A group policy (see
        _gather_group_policy) decides behaviour first; without one the village
        pauses while under attack unless its gather_when_attacked flag is
        armed - a manual override for incomings you judged harmless (e.g. a
        lone scout run). Flip it back off when a real attack is inbound.
        """
        self.units.can_gather = self.get_village_config(
            self.village_id, parameter="gather_enabled", default=False
        )
        policy = self._gather_group_policy()
        if policy == "never":
            self.logger.debug("Scavenging blocked by group policy 'never'")
            return
        under_attack = bool(self.def_man and self.def_man.under_attack)
        if under_attack:
            if policy == "pause_attacked":
                return
            if policy != "always" and not self.get_village_config(
                self.village_id, parameter="gather_when_attacked", default=False
            ):
                return
            self.logger.info(
                "Village under attack but scavenge-under-attack is armed "
                "(%s), scavenging anyway",
                "group policy" if policy == "always" else "gather_when_attacked",
            )
        extra_exclude = self.get_village_config(
            self.village_id, parameter="gather_exclude_units", default=[]
        )
        disabled = list(self.disabled_units) + [u for u in extra_exclude if u not in self.disabled_units]
        self.units.gather(
            selection=self.get_village_config(
                self.village_id, parameter="gather_selection", default=1
            ),
            disabled_units=disabled,
            advanced_gather=self.get_village_config(self.village_id, parameter="advanced_gather", default=1),
            # Never night-consolidate under attack: one long run with every
            # troop is the opposite of what you want with an incoming.
            consolidate=0 if under_attack else self._gather_night_consolidate(),
            reserved=self.troop_reserve,
        )

    def go_manage_market(self):
        """
        Manages the market
        """
        if self.get_config(
                section="market", parameter="auto_trade", default=False
        ) and self.builder.get_level("market"):
            self.logger.info("Managing market")
            # trades_per_hour means what its name says: higher is more trading.
            # The older trade_max_per_hour key does the opposite despite its
            # name (it is multiplied by 3600 into a cooldown, so it really means
            # "hours between trades"), so it is only honoured when the new key
            # is absent and no existing config changes meaning under it.
            try:
                per_hour = float(self.get_config(
                    section="market", parameter="trades_per_hour", default=0) or 0)
            except (TypeError, ValueError):
                per_hour = 0
            if per_hour > 0:
                self.resman.trade_cooldown = int(3600 / per_hour)
            else:
                self.resman.trade_cooldown = int(3600 * self.get_config(
                    section="market", parameter="trade_max_per_hour", default=1
                ))
            self.resman.max_trade_amount = self.get_config(
                section="market", parameter="max_trade_amount", default=4000
            )
            self.resman.trade_max_duration = self.get_config(
                section="market", parameter="max_trade_duration", default=1
            )
            self.resman.trade_round_to_1000 = self.get_config(
                section="market", parameter="trade_round_to_1000", default=False
            )
            if self.get_config(
                    section="market", parameter="trade_multiplier", default=False
            ):
                self.resman.trade_bias = self.get_config(
                    section="market", parameter="trade_multiplier_value", default=1.0
                )
            self.resman.manage_market(
                drop_existing=self.get_config(
                    section="market", parameter="auto_remove", default=True
                )
            )

        res = self.wrapper.get_action(village_id=self.village_id, action="overview")
        self.game_data = Extractor.game_state(res)
        self.resman.update(self.game_data)
        # Premium trading is gated by the account-wide Market toggle (the behaviour
        # switch) AND the per-village toggle (which villages do it). world.
        # trade_for_premium is now just a capability marker and no longer gates this.
        if self.get_config(
                section="market", parameter="trade_for_premium", default=False
        ) and self.get_village_config(
            self.village_id, parameter="trade_for_premium", default=False
        ):
            # Set the parameter correctly when the config says so.
            self.resman.do_premium_trade = True
            self.resman.do_premium_stuff()

    def run(self, config=None, first_run=False):
        # setup and check if village still exists / is accessible
        self.config = config
        self.wrapper.delay = self.get_config(
            section="bot", parameter="delay_factor", default=1.0
        )

        data = self.village_init()

        if not self.game_data:
            self.logger.error(
                "Error reading game data for village %s", self.village_id
            )
            raise VillageInitException

        self.set_world_config()

        vdata = self.get_config(section="villages", parameter=self.village_id)
        if not vdata:
            raise VillageInitException

        if not self.get_village_config(
                self.village_id, parameter="managed", default=False
        ):
            return False

        self.update_pre_run()

        self.setup_defence_manager(data=data)
        self.run_quest_actions(config=config)

        self.do_scavenge_unlock()
        self.run_builder()
        self.units_get_template()
        self.set_unit_wanted_levels()

        self.units.update_totals()
        # Dodge the fragile units now that the troop counts are fresh. This is
        # deliberately independent of the global units.manage_defence switch:
        # the per-village toggle alone decides.
        if self.def_man and self.def_man.under_attack and self.def_man.auto_evacuate:
            if self.def_man.evacuate():
                self.units.update_totals()
        self.run_unit_upgrades()
        self.run_snob_recruit()
        self.do_recruit()
        self.manage_local_resources()
        self.run_balancer()

        # Refresh forced-peace state before farming so run_farming() can skip
        # sending attacks during (or arriving into) a configured peace window.
        self.check_forced_peace()
        can_attack = self.setup_attack_manager()
        # The shaper runs before scavenging because both want axes and the shaper
        # only ever takes a capped slice; scavenging then sweeps up whatever is
        # left. The farm pass is the slow part of the cycle (one request per
        # target) and only uses cavalry, so it goes last - waiting on it just
        # kept the scavenge runs sitting at home.
        if can_attack:
            self.run_barb_shaper()
        self.do_gather()
        if can_attack:
            self.run_farming()

        self.go_manage_market()

        self.set_cache_vars()
        self.logger.info("Village cycle done, returning to overview")
        self.wrapper.reporter.report(
            self.village_id, "TWB_POST_RESOURCE", str(self.resman.actual)
        )
        self.wrapper.reporter.add_data(
            self.village_id,
            data_type="village.resources",
            data=json.dumps(self.resman.actual),
        )
        self.wrapper.reporter.add_data(
            self.village_id,
            data_type="village.buildings",
            data=json.dumps(self.builder.levels),
        )
        self.wrapper.reporter.add_data(
            self.village_id,
            data_type="village.troops",
            data=json.dumps(self.units.total_troops),
        )
        self.wrapper.reporter.add_data(
            self.village_id, data_type="village.config", data=json.dumps(vdata)
        )

    @staticmethod
    def _quest_completed(res):
        """True when a quest_complete response says the quest was actually closed.
        A refused completion still answers with a JSON body, but carries an
        error - and get_api_action() hands back any parsed body, so simply
        testing the response for truth counted those refusals as successes."""
        if not res or not isinstance(res, dict):
            return False
        if res.get("error") or res.get("errors") or res.get("error_code"):
            return False
        return True

    def get_quests(self, skip=()):
        """
        Complete one finished quest and return its id, or None when there was
        nothing to complete. ``skip`` holds the quest ids already attempted this
        cycle so a quest the game will not close is only tried once.
        """
        result = Extractor.get_quests(self.wrapper.last_response, skip=skip)
        if not result:
            self.logger.debug("There where no completed quests")
            return None
        qres = self.wrapper.get_api_action(
            action="quest_complete",
            village_id=self.village_id,
            params={"quest": result, "skip": "false"},
        )
        if not self._quest_completed(qres):
            self.logger.info(
                "Quest %s reports finished but the game did not close it: %s",
                str(result),
                qres,
            )
            return None
        self.logger.info("Completed quest: %s", str(result))
        return result

    def get_quest_rewards(self):
        result = self.wrapper.get_api_data(
            action="quest_popup",
            village_id=self.village_id,
            params={"screen": 'new_quests', "tab": "main-tab", "quest": 0},
        )
        # A failed request (None) or a response without the expected dialog means
        # there is nothing to collect this cycle; bail instead of crashing.
        if not isinstance(result, dict) or "dialog" not in (result.get("response") or {}):
            self.logger.debug("No quest reward dialog returned")
            return False
        # The data is escaped for JS, so unescape it before sending it to the extractor.
        rewards = Extractor.get_quest_rewards(decode(result["response"]["dialog"], 'unicode-escape'))
        for reward in rewards:
            # First check if there is enough room for storing the reward
            for t_resource in reward["reward"]:
                if self.resman.storage - self.resman.actual[t_resource] < reward["reward"][t_resource]:
                    self.logger.info("Not enough room to store the %s part of the reward", t_resource)
                    return False

            qres = self.wrapper.post_api_data(
                action="claim_reward",
                village_id=self.village_id,
                params={"screen": "new_quests"},
                data={"reward_id": reward["id"]}
            )
            if qres:
                if not qres['response']:
                    self.logger.debug("Error getting reward! %s", qres)
                    return False
                else:
                    self.logger.info("Got quest reward: %s", str(reward))
                    for t_resource in reward["reward"]:
                        self.resman.actual[t_resource] += reward["reward"][t_resource]

        self.logger.debug("There where no (more) quest rewards")
        return len(rewards) > 0

    def set_cache_vars(self):
        gv = self.game_data.get("village", {}) if self.game_data else {}

        def _prod_per_hour(key):
            try:
                return int(round(float(gv.get(key, 0)) * 3600))
            except (TypeError, ValueError):
                return 0

        village_entry = {
            "name": self.game_data["village"]["name"],
            "public": self.area.in_cache(self.village_id) if self.area else None,
            "resources": self.resman.actual,
            "required_resources": self.resman.requested,
            "available_troops": self.units.troops,
            "buidling_levels": self.builder.levels,
            "building_queue": self.builder.queue,
            "active_building_queue": getattr(self.builder, "queue_count_ingame", 0),
            "building_queue_ingame": getattr(self.builder, "queue_ingame", []) or [],
            "troops": self.units.total_troops,
            "under_attack": self.def_man.under_attack,
            # Exact capacity/pop/production straight from the game (per-hour for
            # production; the live rate already includes world speed).
            "storage_max": gv.get("storage_max"),
            "pop_used": gv.get("pop"),
            "pop_max": gv.get("pop_max"),
            "production": {
                "wood": _prod_per_hour("wood_prod"),
                "stone": _prod_per_hour("stone_prod"),
                "iron": _prod_per_hour("iron_prod"),
            },
            "scavenge_state": getattr(self.units, "scavenge_state", None),
            "last_run": int(time.time()),
        }
        FileManager.save_json_file(village_entry, f"cache/managed/{self.village_id}.json")
