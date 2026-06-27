import json
import logging
import os
import sys
import time

from core.filemanager import FileManager
from game.attack import AttackCache
from game.reports import ReportCache


class VillageManager:
    @staticmethod
    def farm_manager(verbose=False, clean_reports=False, prune_after_days=0):
        logger = logging.getLogger("FarmManager")
        # Read through FileManager so the active world's config is found
        # (worlds/<name>/config.json), not just the project-root config.json.
        config = FileManager.load_json_file("config.json")
        if not config:
            logger.warning("No config.json found for the active world; skipping farm manager")
            return

        if verbose:
            logger.info("Villages: %d", len(config["villages"]))
        attacks = AttackCache.cache_grab()
        reports = ReportCache.cache_grab()

        if verbose:
            logger.info("Reports: %d", len(reports))
            logger.info("Farms: %d", len(attacks))
        t = {"wood": 0, "iron": 0, "stone": 0}
        # Index attack reports by destination in a single pass so each farm
        # below is an O(1) lookup instead of re-scanning every report. Without
        # this the loop is O(farms x reports) every cycle and only gets slower
        # as the report history grows.
        reports_by_dest = {}
        for rep in reports:
            report = reports[rep]
            if report["type"] == "attack":
                reports_by_dest.setdefault(report["dest"], []).append(report)

        for farm in attacks:
            data = attacks[farm]

            num_attack = []
            loot = {"wood": 0, "iron": 0, "stone": 0}
            total_loss_count = 0
            total_sent_count = 0
            for report in reports_by_dest.get(farm, []):
                for unit in report["extra"]["units_sent"]:
                    total_sent_count += report["extra"]["units_sent"][unit]
                for unit in report["extra"]["units_losses"]:
                    total_loss_count += report["extra"]["units_losses"][unit]
                try:
                    res = report["extra"]["loot"]
                    for r in res:
                        loot[r] = loot[r] + int(res[r])
                        t[r] = t[r] + int(res[r])
                    num_attack.append(report)
                except (KeyError, ValueError, TypeError) as exc:
                    # Malformed/partial loot (missing key, non-numeric value) -
                    # skip this report but leave a trace instead of swallowing it.
                    logger.debug("Skipping report with bad loot data on farm %s: %s", farm, exc)
            percentage_lost = 0

            if total_sent_count > 0:
                percentage_lost = total_loss_count / total_sent_count * 100

            perf = ""
            if "high_profile" in data and data["high_profile"]:
                perf = "High Profile "
            if "low_profile" in data and data["low_profile"]:
                perf = "Low Profile "
            if verbose:
                logger.info(
                    "%sFarm village %s attacked %d times - Total loot: %s - Total units lost: %d (%.2f)",
                    perf, farm, len(num_attack), str(loot), total_loss_count, percentage_lost
                )
            if len(num_attack):
                total = 0
                for k in loot:
                    total += loot[k]
                if len(num_attack) > 3:
                    if total / len(num_attack) < 100 and (
                            "low_profile" not in data or not data["low_profile"]
                    ):
                        if verbose:
                            logger.info(
                                "Farm %s has very low resources (%d avg total), extending farm time",
                                farm, total / len(num_attack)
                            )
                        data["low_profile"] = True
                        AttackCache.set_cache(farm, data)
                    elif total / len(num_attack) > 500 and (
                            "high_profile" not in data or not data["high_profile"]
                    ):
                        if verbose:
                            logger.info(
                                "Farm %s has very high resources (%d avg total), setting to high profile",
                                farm, total / len(num_attack)
                            )
                        data["high_profile"] = True
                        AttackCache.set_cache(farm, data)

            if percentage_lost > 20 and not data.get("low_profile"):
                logger.warning(f"Dangerous {percentage_lost} percentage lost units! Extending farm time")
                data["low_profile"] = True
                data["high_profile"] = False
                AttackCache.set_cache(farm, data)
            if percentage_lost > 50 and len(num_attack) > 10:
                logger.critical("Farm seems too dangerous/ unprofitable to farm. Setting safe to false!")
                data["safe"] = False
                AttackCache.set_cache(farm, data)

        if verbose:
            logger.info("Total loot: %s" % t)

        # Opt-in cleanup of farm targets we have not attacked in a long time
        # (out of range, superseded, etc). Off by default (0). A still-valid
        # target just gets a fresh cache entry next time it is attacked, so this
        # only sheds dead weight - never targets parked as unsafe, which must
        # stay so the bot does not re-engage a dangerous village.
        if prune_after_days:
            cutoff = int(time.time()) - int(prune_after_days) * 86400
            pruned = 0
            for farm, data in attacks.items():
                if data.get("safe", True) is False:
                    continue
                last = data.get("last_attack", 0)
                if last and last < cutoff:
                    AttackCache.remove(farm)
                    pruned += 1
            if pruned:
                logger.info(
                    "Pruned %d stale farm target(s) with no attack in %d+ days",
                    pruned, int(prune_after_days),
                )

        if clean_reports:
            reports_dir = FileManager._resolve("cache/reports")
            list_of_files = sorted([os.path.join(reports_dir, f) for f in os.listdir(reports_dir)],
                                   key=os.path.getctime)

            logger.info(f"Found {len(list_of_files)} files")

            while len(list_of_files) > clean_reports:
                oldest_file = list_of_files.pop(0)
                logger.info(f"Delete old report ({oldest_file})")
                os.remove(os.path.abspath(oldest_file))


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout)
    VillageManager.farm_manager(verbose=True)
