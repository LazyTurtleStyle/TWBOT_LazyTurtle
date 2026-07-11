"""
Barb shaper (alpha)
Knocks down the walls of nearby barbarian villages with axe+ram attacks so the
normal farm loop can farm them without bleeding light cavalry on the wall.

Community-verified wall mechanics this is built on (see also game/simulator.py):
- Rams do pre-battle damage: the fight happens at a reduced wall level, but
  never below half the original level (rounded up).
- Winning the fight downgrades the wall afterwards by roughly
  rams / (2 * 1.09^wall) levels, so a clean full raze of level W needs about
  2 * W * 1.09^W rams (e.g. wall 10 -> ~48, wall 20 -> ~225).
- An empty village still fights back through the wall's basic defense
  (20 * 1.25^level during the battle), which is what kills unescorted troops.
  The axe escort is sized so even a worst-case luck roll (-25%) keeps the
  expected losses under a configurable tolerance.
"""

import logging
import math
import time

from core.filemanager import FileManager
from game.simulator import Simulator


class BarbShaper:
    """
    Sends wall-razing attacks at the closest barbs whose last scout report
    shows a wall above the configured level. Only ever acts on villages the
    report loop has already proven empty (safe_to_engage), and re-arms a
    target only once a report newer than our own hit still shows a wall.
    """
    logger = logging.getLogger("BarbShaper")
    sim = Simulator()
    STATE_FILE = "cache/barbshaper.json"
    # Extra rams on top of the theoretical raze requirement - covers rounding
    # and the ram losses the wall itself causes during the fight.
    RAM_SAFETY = 1.1
    # Never send fewer axes than this, however low the wall: the report the
    # escort size is based on can be hours old.
    MIN_AXES = 20

    def __init__(self, wrapper=None, village_id=None, troopmanager=None,
                 map=None, repman=None, attack_manager=None):
        self.wrapper = wrapper
        self.village_id = village_id
        self.troopmanager = troopmanager
        self.map = map
        self.repman = repman
        self.attack_manager = attack_manager

        # Config, set by the village loop before run()
        self.min_wall = 2
        self.loss_tolerance = 1.0
        self.max_sends = 2
        self.ram_reserve = 0
        self.report_max_age_hours = 24
        self.scavenge_uses_axes = False

    # ------------------------------------------------------------------ math

    @classmethod
    def rams_to_raze(cls, wall):
        """Rams needed to bring a wall from `wall` to 0 in one clean win."""
        if wall <= 0:
            return 0
        return int(math.ceil(2 * wall * math.pow(1.09, wall) * cls.RAM_SAFETY))

    @classmethod
    def wall_during_battle(cls, rams, wall):
        """Wall level the fight is actually fought at: rams' pre-battle damage,
        capped at half the original level (the game never lets pre-damage go
        further than that)."""
        return max(cls.sim.pre_wall(num_rams=rams, wall=wall),
                   int(math.ceil(wall / 2)))

    @classmethod
    def expected_losses(cls, axes, rams, wall):
        """Average attacker deaths for an axe+ram hit on an empty village,
        computed at worst-case luck (-25%)."""
        during = cls.wall_during_battle(rams, wall)
        losses = cls.sim.simulate_against_wall(
            {"axe": axes, "ram": rams}, during, luck=-25)
        return sum(losses.values())

    @classmethod
    def axes_needed(cls, rams, wall, tolerance, axes_available):
        """Smallest axe escort keeping worst-case losses under `tolerance`,
        or None if even every axe we have isn't enough. Losses shrink
        monotonically as the escort grows, so binary search."""
        if axes_available < cls.MIN_AXES:
            return None
        if cls.expected_losses(axes_available, rams, wall) > tolerance:
            return None
        lo, hi = cls.MIN_AXES, axes_available
        while lo < hi:
            mid = (lo + hi) // 2
            if cls.expected_losses(mid, rams, wall) > tolerance:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # ----------------------------------------------------------------- state

    @classmethod
    def get_state(cls):
        return FileManager.load_json_file(cls.STATE_FILE) or {}

    @classmethod
    def set_state(cls, state):
        FileManager.save_json_file_atomic(state, cls.STATE_FILE)

    # ------------------------------------------------------------------- run

    def run(self):
        if self.scavenge_uses_axes:
            # The user's axes belong to scavenging; never compete with it.
            self.logger.info(
                "%s: scavenging is set to use axes (axe not in "
                "gather_exclude_units) - barb shaper stays idle", self.village_id)
            return
        if not self.attack_manager or not self.map or not self.repman:
            return

        troops = {k: int(v) for k, v in (self.troopmanager.troops or {}).items()}
        axes_home = troops.get("axe", 0)
        rams_home = max(0, troops.get("ram", 0) - int(self.ram_reserve))
        if not rams_home or not axes_home:
            self.logger.debug(
                "%s: no rams/axes at home for shaping", self.village_id)
            return

        if not self.attack_manager.targets:
            # The farm pass is rate-limited and may not have run this cycle;
            # building the distance-sorted target list is map-cache only.
            self.attack_manager.get_targets()

        state = self.get_state()
        sends = 0
        for target, distance in self.attack_manager.targets:
            if sends >= self.max_sends:
                break
            vid = target["id"]
            if target.get("owner") != "0":
                continue

            report_id, extra, age = self.repman.latest_for(vid, report_type="scout")
            if report_id is None or age is None:
                continue
            if age > self.report_max_age_hours * 3600:
                continue
            wall = ((extra.get("buildings") or {}).get("wall"))
            if wall is None:
                continue
            wall = int(wall)
            entry = state.get(str(vid))
            report_when = int(extra.get("when") or 0)
            if entry and report_when <= int(entry.get("sent_at", 0)):
                # No report newer than our own hit yet - rams may still be
                # walking, or the result hasn't been scouted. Don't double up.
                continue
            if wall <= int(self.min_wall):
                if entry and entry.get("status") == "sent":
                    entry["status"] = "done"
                    entry["wall_after"] = wall
                    state[str(vid)] = entry
                continue
            if self.repman.safe_to_engage(vid) != 1:
                self.logger.debug(
                    "%s: skipping %s, last report shows defenders", self.village_id, vid)
                continue

            rams = self.rams_to_raze(wall)
            if rams > rams_home:
                self.logger.info(
                    "%s: wall %d at %s needs %d rams, only %d available - skipping",
                    self.village_id, wall, vid, rams, rams_home)
                continue
            axes = self.axes_needed(rams, wall, float(self.loss_tolerance), axes_home)
            if axes is None:
                self.logger.info(
                    "%s: not enough axes to escort %d rams into wall %d at %s "
                    "within loss tolerance %.1f", self.village_id, rams, wall,
                    vid, float(self.loss_tolerance))
                continue

            send = {"axe": axes, "ram": rams}
            if troops.get("spy", 0) >= 1:
                # A surviving spy makes the result report show the new wall level.
                send["spy"] = 1
            result = self.attack_manager.attack(vid, troops=send)
            if result == "forced_peace":
                break
            if not result or (isinstance(result, dict) and result.get("error")):
                self.logger.warning(
                    "%s: shaper attack on %s was refused: %s",
                    self.village_id, vid, result)
                continue

            self.logger.info(
                "Barb shaper: %s -> %s wall %d with %d rams + %d axes "
                "(expected losses %.2f at worst-case luck)",
                self.village_id, vid, wall, rams, axes,
                self.expected_losses(axes, rams, wall))
            self.wrapper.reporter.report(
                self.village_id, "TWB_SHAPER",
                "Barb shaper %s -> %s wall %d (%d rams, %d axes)" % (
                    self.village_id, vid, wall, rams, axes))
            location = target.get("location") or []
            state[str(vid)] = {
                "status": "sent",
                "sent_at": int(time.time()),
                "source": self.village_id,
                "wall": wall,
                "rams": rams,
                "axes": axes,
                "distance": round(float(distance), 1),
                "coords": list(location),
                "name": target.get("name", ""),
            }
            axes_home -= axes
            rams_home -= rams
            troops["axe"] = axes_home
            troops["ram"] = rams_home + int(self.ram_reserve)
            sends += 1
            if not rams_home or axes_home < self.MIN_AXES:
                break

        self.set_state(state)
