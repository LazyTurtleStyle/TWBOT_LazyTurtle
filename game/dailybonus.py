"""Claim the daily login bonus chests.

The daily-bonus screen carries a DailyBonus.init(...) call whose first
argument is the current reward cycle: chests keyed by day, one unlocking per
login day (structure verified live on nl99). A chest is claimable when it is
unlocked and not yet collected; claiming posts ajaxaction=open with the
chest's day number, exactly like the in-game DailyBonus.openChest. Chests
that are still locked (future days, or the premium-point unlocks) are left
alone.
"""

import logging
from datetime import date

from core.extractors import Extractor
from core.filemanager import FileManager

CACHE = "cache/daily_bonus.json"

logger = logging.getLogger("DailyBonus")


def run(wrapper, village_id):
    """Visit the daily-bonus screen once per calendar day and claim every
    claimable chest. On any failure the day is not marked done, so the next
    bot cycle retries."""
    if not village_id:
        return
    state = FileManager.load_json_file(CACHE) or {}
    today = date.today().isoformat()
    if state.get("last_check") == today:
        return

    res = wrapper.get_url(
        f"game.php?village={village_id}&screen=daily_bonus")
    if res is None:
        return
    data = Extractor.daily_bonus_data(res)
    if not data:
        # Feature not on this world / page did not render the bonus screen.
        logger.debug("No daily-bonus data on the screen")
        state["last_check"] = today
        FileManager.save_json_file(state, CACHE)
        return

    claimed = []
    for day, chest in sorted(
        (data.get("chests") or {}).items(), key=lambda kv: int(kv[0])
    ):
        if chest.get("is_locked") or chest.get("is_collected"):
            continue
        result = wrapper.get_api_action(
            village_id=village_id,
            action="open",
            params={"screen": "daily_bonus"},
            data={"day": str(chest.get("day") or day),
                  "from_screen": "daily_bonus"},
        )
        if not result:
            logger.warning("Failed to claim daily bonus chest %s, will retry "
                           "next cycle", day)
            return
        name = ((chest.get("reward") or {}).get("item") or {}).get("name") \
            or f"chest {day}"
        claimed.append(name)
        logger.info("Claimed daily bonus chest %s: %s", day, name)

    state["last_check"] = today
    if claimed:
        state["last_claimed"] = {"date": today, "rewards": claimed}
    FileManager.save_json_file(state, CACHE)
