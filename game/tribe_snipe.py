"""
Tribe snipe: support-snipes for a tribemate's village.

The Snipe tab works off the bot's own incoming list, which only ever holds
attacks on this account's villages. A tribemate under attack has to tell you
where and when instead, so this keeps a short list of hand-typed targets -
coordinates plus the attack's arrival, to the millisecond - and each one opens
the Snipe tab's options panel exactly like an incoming does.

Nothing here fires anything. Arming goes through the ordinary support-snipe
queue (cache/snipes.json) and the ordinary snipe runner, with the typed
coordinates as the target; the engine never cared whose village it lands in.

Storage uses the scheduler's locked list helpers on its own file, same as the
snipe queues. Only the dashboard writes it.
"""

import time
import uuid

from core.filemanager import FileManager
from game import attack_scheduler

TARGETS_FILE = "cache/tribe_targets.json"

# A target is kept this long after its hit, so the result of whatever was
# armed against it is still next to it when you come back to look.
KEEP_AFTER_HIT_SECONDS = 3600


def _path(path=None):
    return path or FileManager.get_path(TARGETS_FILE)


def load_targets(path=None):
    """Every stored target (always a list), soonest hit first."""
    targets = attack_scheduler.load_schedule(path=_path(path))
    return sorted(targets, key=lambda t: int(t.get("arrival_ms") or 0))


def add_target(x, y, arrival_ms, label="", village=None, path=None):
    """Store one target and return it. village is whatever the map cache knows
    about the coordinates ({id, name, points, owner}), or None."""
    village = village or {}
    entry = {
        "id": uuid.uuid4().hex[:12],
        "created": int(time.time()),
        "x": int(x), "y": int(y),
        "arrival_ms": int(arrival_ms),
        "label": str(label or "")[:80],
        "village_id": str(village["id"]) if village.get("id") else None,
        "village_name": village.get("name") or None,
        "points": village.get("points"),
        "owner": str(village["owner"]) if village.get("owner") else None,
    }

    def mut(targets):
        targets.append(entry)
    attack_scheduler.update(mut, path=_path(path))
    return entry


def remove_target(target_id, path=None):
    """Drop one target. Snipes already armed against it are not touched -
    they live in the snipe queue and are cancelled from there."""
    removed = []

    def mut(targets):
        keep = [t for t in targets if t.get("id") != target_id]
        removed.append(len(keep) != len(targets))
        targets[:] = keep
    attack_scheduler.update(mut, path=_path(path))
    return bool(removed and removed[0])


def prune(path=None, now=None):
    """Forget targets whose hit is more than KEEP_AFTER_HIT_SECONDS gone."""
    cutoff_ms = int(((now or time.time()) - KEEP_AFTER_HIT_SECONDS) * 1000)

    def mut(targets):
        targets[:] = [t for t in targets
                      if int(t.get("arrival_ms") or 0) >= cutoff_ms]
    attack_scheduler.update(mut, path=_path(path))
