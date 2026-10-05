"""
Snipe wave: "queue every option, stop once it holds".

During a noble wave the support snipe is a numbers game. A send lands within
roughly +-15-20ms of its aim (network jitter the clock sync cannot remove), so
a window of a few dozen ms keeps about half the attempts. A miss is recalled
seconds after it leaves, so its troops are home again almost at once. The
cheap way to cover a train is therefore to arm every reachable option, keep
the first one that lands inside, and cancel the rest.

Measured on nl116 on 2026-10-05 (about 90 nobles, 380 attacks, 2 villages
lost) with a prototype of this module driven from outside the bot.

While a wave is switched on, the snipe runner calls tick() every
TICK_SECONDS. A tick is cache-only (no requests) and does three things:

  1. cancels armed wave snipes a hit has made unnecessary, and those that are
     bound to fail because their troops already stand in a kept snipe;
  2. if "skip villages being nobled" is on, cancels the wave snipes those
     villages were armed to send;
  3. plans every reachable village onto every open noble and arms the result.

Which nobles: incomings whose label marks them as a noble (the speed tag the
tagger writes, see NOBLE_LABELS) on a village the wave covers. Trains with the
snipe tag in their label come first; untagged trains are planned last, or not
at all. Labels containing the exclude tag (the c-snipe trigger) are skipped.

Coverage: a train is a run of nobles on one village with no other command
between them. A support standing behind the train's first noble meets every
noble after it, so a hit there covers the rest of the train. A hit in front of
the first noble only covers that noble: a front-loaded first noble (sent with
an army) kills whatever stands in front of it. With prefer_behind_first off,
a hit on any noble covers that noble and the rest of its train.
"""

import logging
import math
import time
import uuid

from core.filemanager import FileManager
from game import snipe as snipe_engine
from game.incomings import INCOMINGS_DIR, load_world_speeds, unit_travel_seconds

logger = logging.getLogger("SnipeWave")

WAVE_FILE = "cache/snipe_wave.json"
TROOPS_FILE = "cache/troops_moving.json"
TICK_SECONDS = 20

# The speed tags the incoming tagger writes for a noble-speed command.
NOBLE_LABELS = ("edel", "noble", "snob")

DEFAULTS = {
    "enabled": False,
    # Trains whose label carries this text are planned first.
    "tag": "SNIPE THIS",
    # Labels carrying this text are left alone (the c-snipe trigger).
    "exclude_tag": "C-SNIPE",
    # Plan untagged noble trains too, after the tagged ones.
    "include_untagged": True,
    # Stop once this many snipes kept on a noble (or behind a train's first).
    "keep_hits": 1,
    "prefer_behind_first": True,
    # Keep window: the whole gap before the noble, at most this wide each way.
    "max_keep_ms": 50,
    # Gaps whose keep window comes out narrower than this are skipped.
    "min_keep_ms": 4,
    # A noble with no other command on its village in the 1.5s before it
    # gets a gap this wide in front of it.
    "lone_gap_ms": 100,
    "attempts_per_noble": 10,
    # Options per village.
    "use_sword_pace": True,
    "use_spear_only": True,
    "use_catapult_pace": True,
    "use_heavy": True,
    "heavy_min": 250,
    "heavy_max": 1000,
    "infantry_min": 800,
    "infantry_max": 2000,
    "sword_max": 1000,
    "skip_nobled": True,
    # Spacing: any two sends, and one village's sends of one troop pool.
    "min_gap_s": 20,
    "village_gap_s": 120,
    # Sends closer than this are not planned (prestage + clock sync).
    "min_lead_s": 300,
    "min_pct": 80,
}


def _now():
    return time.time()


def load_state(path=None):
    data = FileManager.load_json_file(path or WAVE_FILE) or {}
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in (data.get("settings") or {}).items()
                     if k in DEFAULTS})
    if "enabled" in data:
        settings["enabled"] = bool(data["enabled"])
    return settings, data.get("status") or {}


def save_settings(settings, path=None):
    """Store the settings (unknown keys dropped, types coerced to the
    defaults'), keeping the last status. Returns the stored settings."""
    data = FileManager.load_json_file(path or WAVE_FILE) or {}
    clean = {}
    for key, default in DEFAULTS.items():
        if key not in settings:
            continue
        value = settings[key]
        try:
            if isinstance(default, bool):
                value = value in (True, "true", "on", "1", 1)
            elif isinstance(default, int):
                value = int(float(value))
            else:
                value = str(value)
        except (TypeError, ValueError):
            continue
        clean[key] = value
    merged = dict(data.get("settings") or {})
    merged.update({k: v for k, v in clean.items() if k != "enabled"})
    data["settings"] = merged
    if "enabled" in clean:
        data["enabled"] = clean["enabled"]
    FileManager.save_json_file_atomic(data, path or WAVE_FILE)
    out = dict(DEFAULTS)
    out.update(merged)
    out["enabled"] = bool(data.get("enabled"))
    return out


def _save_status(status, path=None, enabled=None):
    data = FileManager.load_json_file(path or WAVE_FILE) or {}
    data["status"] = status
    if enabled is not None:
        data["enabled"] = enabled
    FileManager.save_json_file_atomic(data, path or WAVE_FILE)


# -- pure planning -----------------------------------------------------------

def is_noble(label):
    return (label or "").strip().lower().startswith(NOBLE_LABELS)


def trains(incomings):
    """{target_id: [[noble, ...], ...]}: each inner list is one train, nobles
    on that village with no other command between them, soonest first."""
    by_target = {}
    for inc in incomings:
        if inc.get("arrival_ms"):
            by_target.setdefault(str(inc.get("target_id")), []).append(inc)
    out = {}
    for tid, cmds in by_target.items():
        cmds.sort(key=lambda c: c["arrival_ms"])
        groups, cur = [], []
        for c in cmds:
            if is_noble(c.get("game_label")):
                cur.append(c)
            elif cur:
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        if groups:
            out[tid] = groups
    return out


def window(incomings, target_id, hit_ms, settings):
    """(aim_ms, keep_ms, lo, hi) for a support landing in front of hit_ms on
    target_id: the middle of the gap since the previous command there."""
    prev = [c["arrival_ms"] for c in incomings
            if str(c.get("target_id")) == str(target_id)
            and c.get("arrival_ms") and c["arrival_ms"] < hit_ms]
    last = max(prev) if prev else None
    if last is not None and hit_ms - last < 1500:
        lo = last + 1
    else:
        lo = hit_ms - int(settings["lone_gap_ms"])
    hi = hit_ms - 1
    aim = (lo + hi) // 2
    keep = max(0, min(int(settings["max_keep_ms"]), (hi - lo) // 2))
    return aim, keep, lo, hi


def kept_hits(snipes):
    """{hit_ms: count} of snipes that kept (landed and stayed)."""
    out = {}
    for s in snipes:
        if s.get("status") == "done" and s.get("hit_ms") \
                and "could NOT be recalled" not in (s.get("result") or ""):
            h = int(s["hit_ms"])
            out[h] = out.get(h, 0) + 1
    return out


def covered(groups, kept, settings):
    """Set of noble arrival_ms values that need no more snipes."""
    need = max(1, int(settings["keep_hits"]))
    done = set()
    for group in groups:
        hits = [n["arrival_ms"] for n in group]
        if settings["prefer_behind_first"] and len(hits) > 1:
            if hits[0] in kept and kept[hits[0]] >= need:
                done.add(hits[0])
            if sum(kept.get(h, 0) for h in hits[1:]) >= need:
                done.update(hits)
        else:
            # a hit on noble k also stands against every later noble
            for i, h in enumerate(hits):
                if sum(kept.get(x, 0) for x in hits[:i + 1]) >= need:
                    done.add(h)
    return done


def _pools(units):
    return "hc" if units.get("heavy") else "inf"


def options(own, settings, inf_free, hc_free):
    """(pace_unit, units, pool) choices for one village, preferred first."""
    out = []
    spear, sword = int(own.get("spear", 0) or 0), int(own.get("sword", 0) or 0)
    cap = int(settings["infantry_max"])
    if inf_free:
        sw = min(sword, int(settings["sword_max"]), cap)
        if settings["use_sword_pace"] and sw > 0:
            out.append(("sword", {"spear": min(spear, cap - sw), "sword": sw}, "inf"))
        if settings["use_spear_only"] and spear > 0:
            out.append(("spear", {"spear": min(spear, cap)}, "inf"))
        if settings["use_catapult_pace"] and int(own.get("catapult", 0) or 0) >= 1:
            half = cap // 2
            out.append(("catapult", {"spear": min(spear, half), "sword": min(sword, half),
                                     "catapult": 1}, "inf"))
    if hc_free and settings["use_heavy"]:
        heavy = min(int(own.get("heavy", 0) or 0), int(settings["heavy_max"]))
        if heavy >= int(settings["heavy_min"]):
            out.append(("heavy", {"heavy": heavy}, "hc"))
    good = []
    for pace, units, pool in out:
        units = {u: n for u, n in units.items() if n > 0}
        foot = sum(n for u, n in units.items() if u != "catapult")
        if pool == "inf" and foot < int(settings["infantry_min"]):
            continue
        good.append((pace, units, pool))
    return good


def plan(incomings, snipes, own_villages, troops, speeds, settings, now):
    """Entries to arm. own_villages: {id: {"name", "location"}}; troops:
    {id: {unit: count}} at home; speeds: (world_speed, unit_speed, {unit:
    base minutes per field})."""
    ws, us, base = speeds
    tag = (settings["tag"] or "").lower()
    excl = (settings["exclude_tag"] or "").lower()
    groups_by_target = trains(incomings)
    kept = kept_hits(snipes)
    live = [s for s in snipes if s.get("status") in ("armed", "running")]
    attempts = {}
    for s in live:
        if s.get("hit_ms"):
            attempts[int(s["hit_ms"])] = attempts.get(int(s["hit_ms"]), 0) + 1
    sends = [float(s.get("send_est_ts") or 0) for s in live]
    vsends = {}
    for s in live:
        key = (s["village_id"], _pools(s.get("units") or {}))
        vsends.setdefault(key, []).append(float(s.get("send_est_ts") or 0))
    away = {"inf": set(), "hc": set()}
    for s in snipes:
        if s.get("status") == "done":
            sent = s.get("units_sent") or s.get("units") or {}
            if sent.get("spear") or sent.get("sword"):
                away["inf"].add(str(s["village_id"]))
            if sent.get("heavy"):
                away["hc"].add(str(s["village_id"]))
    targets = set(groups_by_target)
    targets_covered = {str(s.get("target_village_id")) for s in snipes
                       if s.get("status") == "done"}

    # every open noble with its window and priority
    nobles = []
    for tid, groups in groups_by_target.items():
        done = covered(groups, kept, settings)
        for group in groups:
            labels = " ".join((n.get("game_label") or "").lower() for n in group)
            tagged = bool(tag) and tag in labels
            if not tagged and not settings["include_untagged"]:
                continue
            for pos, n in enumerate(group):
                label = (n.get("game_label") or "").lower()
                h = n["arrival_ms"]
                if h / 1000.0 <= now or h in done or (excl and excl in label):
                    continue
                aim, keep, lo, hi = window(incomings, tid, h, settings)
                if keep < int(settings["min_keep_ms"]):
                    continue
                if settings["prefer_behind_first"] and len(group) > 1:
                    rank = {1: 0, 2: 1, 0: 2}.get(pos, 3)
                else:
                    rank = pos
                nobles.append({"incoming": n, "target_id": tid, "hit": h,
                               "aim": aim, "keep": keep, "rank": rank,
                               "tagged": tagged, "n": attempts.get(h, 0)})
    nobles.sort(key=lambda x: (not x["tagged"], x["target_id"] in targets_covered,
                               x["rank"], -x["keep"], x["hit"]))

    pool = [v for v in own_villages
            if not (settings["skip_nobled"] and v in targets)
            and own_villages[v].get("location")]
    cap = int(settings["attempts_per_noble"])
    out = []
    for rnd in range(cap):
        for nb in nobles:
            if nb["n"] >= cap:
                continue
            tx, ty = nb["incoming"]["target_coords"]
            pick = None
            for v in pool:
                if v == nb["target_id"]:
                    continue
                vx, vy = own_villages[v]["location"]
                dist = math.hypot(vx - tx, vy - ty)
                for pace, units, upool in options(
                        troops.get(v) or {}, settings,
                        v not in away["inf"], v not in away["hc"]):
                    if not base.get(pace):
                        continue
                    travel = unit_travel_seconds(dist, base[pace], ws, us)
                    send = nb["aim"] / 1000.0 - travel
                    if send - now < int(settings["min_lead_s"]):
                        continue
                    if any(abs(send - x) < int(settings["min_gap_s"]) for x in sends):
                        continue
                    if any(abs(send - x) < int(settings["village_gap_s"])
                           for x in vsends.get((v, upool), [])):
                        continue
                    pick = (v, pace, units, upool, send, travel, dist)
                    break
                if pick:
                    break
            if not pick:
                continue
            v, pace, units, upool, send, travel, dist = pick
            sends.append(send)
            vsends.setdefault((v, upool), []).append(send)
            nb["n"] += 1
            inc = nb["incoming"]
            out.append({
                "id": uuid.uuid4().hex[:12],
                "status": "armed",
                "created": int(now),
                "wave": True,
                "incoming_id": str(inc.get("command_id")),
                "village_id": v,
                "village_name": own_villages[v].get("name") or v,
                "target_village_id": nb["target_id"],
                "target_name": (own_villages.get(nb["target_id"]) or {}).get("name")
                or "%d|%d" % (tx, ty),
                "target_x": int(tx), "target_y": int(ty),
                "tribe_target_id": None,
                "units": units,
                "pace_unit": pace,
                "land_ms": nb["aim"],
                "boost": 1.0,
                "shortfall": "scale",
                "min_pct": int(settings["min_pct"]),
                "max_delta_ms": nb["keep"],
                "hit_ms": nb["hit"],
                "distance": round(dist, 1),
                "travel_est": int(travel),
                "send_est_ts": int(send),
                "start_ts": int(max(now, send - snipe_engine.PRESTAGE_SECONDS)),
            })
    return out


def cancellations(incomings, snipes, settings):
    """[(snipe_id, reason)] of armed wave snipes no longer worth sending."""
    groups_by_target = trains(incomings)
    kept = kept_hits(snipes)
    done = set()
    for groups in groups_by_target.values():
        done |= covered(groups, kept, settings)
    away = {"inf": set(), "hc": set()}
    for s in snipes:
        if s.get("status") == "done":
            sent = s.get("units_sent") or s.get("units") or {}
            if sent.get("spear") or sent.get("sword"):
                away["inf"].add(str(s["village_id"]))
            if sent.get("heavy"):
                away["hc"].add(str(s["village_id"]))
    out = []
    for s in snipes:
        if s.get("status") != "armed" or not s.get("wave"):
            continue
        if s.get("hit_ms") and int(s["hit_ms"]) in done:
            out.append((s["id"], "covered"))
        elif str(s["village_id"]) in away[_pools(s.get("units") or {})]:
            out.append((s["id"], "troops already kept elsewhere"))
        elif settings["skip_nobled"] and str(s["village_id"]) in groups_by_target:
            out.append((s["id"], "sending village is being nobled"))
    return out


# -- the tick ----------------------------------------------------------------

def _load_incomings():
    try:
        names = FileManager.list_directory(INCOMINGS_DIR, ends_with=".json")
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        c = FileManager.load_json_file("%s/%s" % (INCOMINGS_DIR, name))
        if c and c.get("arrival_ms") and c.get("target_coords"):
            out.append(c)
    return out


def _own_villages(village_ids):
    out = {}
    for vid in village_ids:
        data = FileManager.load_json_file("cache/villages/%s.json" % vid) or {}
        loc = data.get("location")
        if loc and len(loc) == 2:
            out[str(vid)] = {"name": data.get("name") or str(vid),
                             "location": [int(loc[0]), int(loc[1])]}
    return out


def _troops_home():
    data = FileManager.load_json_file(TROOPS_FILE) or {}
    return {str(v): (e or {}).get("own") or {}
            for v, e in (data.get("by_village") or {}).items()}


def tick(village_ids, path=None, now=None):
    """One wave pass. Returns the status dict, or None while the wave is off."""
    settings, _ = load_state(path)
    if not settings["enabled"]:
        return None
    now = now or _now()
    incomings = _load_incomings()
    snipes = snipe_engine.load_snipes()

    cancelled = []
    for sid, why in cancellations(incomings, snipes, settings):
        if snipe_engine.disarm(sid) == "disarmed":
            cancelled.append((sid, why))
    if cancelled:
        snipes = snipe_engine.load_snipes()

    own = _own_villages(village_ids)
    entries = plan(incomings, snipes, own, _troops_home(), load_world_speeds(),
                   settings, now)
    for entry in entries:
        snipe_engine.arm(entry)

    groups_by_target = trains(incomings)
    kept = kept_hits(snipes)
    open_nobles = 0
    future = 0
    for groups in groups_by_target.values():
        done = covered(groups, kept, settings)
        for g in groups:
            for n in g:
                if n["arrival_ms"] / 1000.0 > now:
                    future += 1
                    if n["arrival_ms"] not in done:
                        open_nobles += 1
    armed = sum(1 for s in snipe_engine.load_snipes()
                if s.get("status") == "armed" and s.get("wave"))
    status = {"last_tick": int(now), "armed": armed, "open_nobles": open_nobles,
              "future_nobles": future, "armed_now": len(entries),
              "cancelled_now": len(cancelled)}
    enabled = None
    if future == 0:
        # Stopping is safe to automate: nothing is left to snipe.
        enabled = False
        status["stopped"] = "no noble left in the air"
    _save_status(status, path, enabled=enabled)
    if entries or cancelled:
        logger.info("Snipe wave: armed %d, cancelled %d, %d armed in total, "
                    "%d noble(s) still open", len(entries), len(cancelled),
                    armed, open_nobles)
    return status
