import collections
import datetime
import glob
import json
import os
import signal
import uuid
import subprocess
import sys
import threading
import time

import psutil

from game import attack_scheduler

try:
    from game import csnipe
except Exception:  # pragma: no cover - dashboard still works without c-snipe
    csnipe = None

try:
    from game import snipe as snipe_engine
except Exception:  # pragma: no cover - dashboard still works without snipe
    snipe_engine = None

try:
    from game import playerfarm
except Exception:  # pragma: no cover - dashboard still works without it
    playerfarm = None

try:
    from game.incomings import (
        load_world_speeds, travel_table, slowest_floor, rename_command_ingame,
        incoming_session_state, field_distance, unit_travel_seconds,
        DEFAULT_UNIT_SPEEDS, UNIT_ORDER,
    )
except Exception:  # pragma: no cover - dashboard still works without travel times
    load_world_speeds = None
    travel_table = None
    slowest_floor = None
    rename_command_ingame = None
    incoming_session_state = None
    field_distance = None
    unit_travel_seconds = None
    DEFAULT_UNIT_SPEEDS = {}
    UNIT_ORDER = []

# Building display metadata: in-game name + a short chip code, so the build queue
# can be shown as compact icons with a readable mouseover instead of "stone:1".
BUILDING_META = {
    "main": ("Headquarters", "HQ"),
    "barracks": ("Barracks", "BR"),
    "stable": ("Stable", "SB"),
    "garage": ("Workshop", "WS"),
    "watchtower": ("Watchtower", "WT"),
    "smith": ("Smithy", "SM"),
    "place": ("Rally point", "RP"),
    "statue": ("Statue", "SA"),
    "market": ("Market", "MK"),
    "wood": ("Timber camp", "WD"),
    "stone": ("Clay pit", "CL"),
    "iron": ("Iron mine", "IR"),
    "farm": ("Farm", "FM"),
    "storage": ("Warehouse", "WH"),
    "hide": ("Hiding place", "HP"),
    "wall": ("Wall", "WL"),
    "snob": ("Academy", "AC"),
    "church": ("Church", "CH"),
}


def parse_queue_entry(entry):
    """Turn a 'stone:5' queue entry into display data with name + level."""
    building, _, level = str(entry).partition(":")
    name, short = BUILDING_META.get(building, (building.capitalize(), building[:2].upper()))
    return {
        "building": building,
        "level": level,
        "short": short,
        "label": "%s → level %s" % (name, level) if level else name,
    }


class DataReader:
    # Which world's data the dashboard reads, stored per-thread so concurrent
    # requests (the dev server is threaded) never clobber each other's selection.
    # Unset/None = the project root (the default/single-world setup). Set per
    # request from the selected world so one web server can serve several worlds
    # run with `twb.py --world <name>`.
    _world_local = threading.local()

    @staticmethod
    def project_root():
        return os.path.join(os.path.dirname(__file__), "..")

    @staticmethod
    def set_active_world(world):
        """Select which world's config/cache the dashboard reads (None = default)."""
        if world and str(world).strip():
            DataReader._world_local.name = os.path.basename(str(world).strip())
        else:
            DataReader._world_local.name = None

    @staticmethod
    def active_world():
        """The selected world name for this request, or None for the default."""
        return getattr(DataReader._world_local, "name", None)

    @staticmethod
    def list_worlds():
        """Names of configured extra worlds under worlds/ (excludes the default)."""
        wdir = os.path.join(DataReader.project_root(), "worlds")
        if not os.path.isdir(wdir):
            return []
        return sorted(
            name for name in os.listdir(wdir)
            if os.path.isdir(os.path.join(wdir, name))
        )

    @staticmethod
    def create_world(url, user_agent="", cookie=""):
        """Bootstrap a new world from the dashboard (no interactive prompt).

        Parses the in-game URL, writes worlds/<name>/config.json from
        config.example.json with the server endpoint/name and user agent set, and
        optionally seeds a session cookie so the bot can run unattended. Returns
        {"ok": True, "world": name} or {"ok": False, "error": msg}. Never
        overwrites an existing world.
        """
        url = (url or "").strip()
        if "://" not in url:
            return {"ok": False, "error": "Enter the full game URL, e.g. "
                    "https://nl99.tribalwars.nl/game.php?screen=overview"}
        host = url.split("://", 1)[1].split("/")[0]
        endpoint = url.split("?")[0]
        name = os.path.basename(host.split(".")[0].lower().strip())
        if not host or "." not in host or not name:
            return {"ok": False, "error": "That does not look like a valid world URL."}

        world_dir = os.path.join(DataReader.project_root(), "worlds", name)
        config_path = os.path.join(world_dir, "config.json")
        if os.path.exists(config_path):
            return {"ok": False, "error": "World '%s' already exists." % name}

        example_path = os.path.join(DataReader.project_root(), "config.example.json")
        try:
            with open(example_path) as f:
                template = json.load(f, object_pairs_hook=collections.OrderedDict)
        except (OSError, ValueError):
            return {"ok": False, "error": "config.example.json is missing or invalid."}

        template.setdefault("server", {})
        template["server"]["endpoint"] = endpoint
        template["server"]["server"] = name
        ua = (user_agent or "").strip()
        if len(ua) >= 10:
            template.setdefault("bot", {})
            template["bot"]["user_agent"] = ua

        cache_dir = os.path.join(world_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(template, f, indent=2, sort_keys=False)

        cookies = DataReader.parse_cookie_string(cookie)
        if cookies:
            with open(os.path.join(cache_dir, "session.json"), "w") as f:
                json.dump({"endpoint": endpoint, "server": name, "cookies": cookies},
                          f, indent=2)
            with open(os.path.join(cache_dir, "cookies.txt"), "w") as f:
                f.write((cookie or "").strip())
        return {"ok": True, "world": name}

    @staticmethod
    def data_path(*parts):
        """Resolve config.json / cache paths under the active world's data dir."""
        name = DataReader.active_world()
        base = (os.path.join(DataReader.project_root(), "worlds", name)
                if name else DataReader.project_root())
        return os.path.join(base, *parts)

    @staticmethod
    def session_logged_out():
        """True when the incoming poller last recorded a logged-out session for
        the active world (cookie expired). World-aware read for the web process."""
        try:
            p = DataReader.data_path("cache", "world", "incoming_session.json")
            if os.path.exists(p):
                with open(p) as f:
                    return bool((json.load(f) or {}).get("logged_out"))
        except Exception:
            pass
        return False

    # Main loop is considered stalled once its heartbeat is older than this, on
    # top of whatever cycle delay is configured (background threads keep the
    # process and its logs alive on their own schedules even when the main loop
    # is blocked, e.g. waiting out a captcha - see core/request.py).
    HEARTBEAT_GRACE_SECONDS = 900

    @staticmethod
    def watchdog_state():
        """Is the bot's main loop actually turning, or stuck?

        Returns {"stalled": bool, "reason": "captcha"|"heartbeat"|None,
        "since": unix ts or None, "heartbeat_age": seconds or None}.
        A captcha_block.json marker (written by WebWrapper._await_captcha_clear
        while it polls for the solve) is the precise signal, and is removed the
        moment the captcha clears; heartbeat staleness is a generic fallback for
        any other way the main loop could get stuck.
        """
        captcha = DataReader.data_path("cache", "captcha_block.json")
        if os.path.exists(captcha):
            try:
                with open(captcha) as f:
                    since = int((json.load(f) or {}).get("since") or 0)
            except Exception:
                since = None
            return {"stalled": True, "reason": "captcha", "since": since,
                    "heartbeat_age": None}

        heartbeat = DataReader.data_path("cache", "heartbeat.json")
        if not os.path.exists(heartbeat):
            return {"stalled": False, "reason": None, "since": None, "heartbeat_age": None}
        try:
            with open(heartbeat) as f:
                ts = int((json.load(f) or {}).get("ts") or 0)
        except Exception:
            return {"stalled": False, "reason": None, "since": None, "heartbeat_age": None}

        try:
            cfg = DataReader.config_grab().get("bot", {}) or {}
            cycle_delay = max(
                int(cfg.get("active_delay", 0) or 0),
                int(cfg.get("inactive_delay", 0) or 0),
            )
        except Exception:
            cycle_delay = 0
        age = int(time.time()) - ts
        threshold = cycle_delay + DataReader.HEARTBEAT_GRACE_SECONDS
        return {"stalled": age > threshold, "reason": "heartbeat" if age > threshold else None,
                "since": ts, "heartbeat_age": age}

    @staticmethod
    def world_speeds():
        """(world_speed, unit_speed, {unit: base_speed}) for the active world.

        Read via the world-aware data dir (game.incomings.load_world_speeds goes
        through FileManager, which is not world-aware in the web process), with a
        fallback to standard TribalWars speeds when the world data hasn't been
        cached yet.
        """
        world = DataReader.cache_grab("world")  # {filename-without-json: data}
        config = world.get("config") or {}
        units = world.get("unit_info") or {}
        world_speed = float(config.get("speed", 1) or 1)
        unit_speed = float(config.get("unit_speed", 1) or 1)
        speeds = units.get("speeds") if isinstance(units, dict) else None
        if not speeds:
            speeds = dict(DEFAULT_UNIT_SPEEDS)
        return world_speed, unit_speed, speeds

    @staticmethod
    def cache_grab(cache_location):
        output = {}
        c_path = DataReader.data_path("cache", cache_location)
        if not os.path.isdir(c_path):
            return output
        for existing in os.listdir(c_path):
            existing = str(existing)
            if not existing.endswith(".json"):
                continue
            t_path = DataReader.data_path("cache", cache_location, existing)
            with open(t_path, 'r') as f:
                try:
                    output[existing.replace('.json', '')] = json.load(f)
                except Exception as e:
                    print("Cache read error for %s: %s. Removing broken entry" % (t_path, str(e)))
                    f.close()
                    os.remove(t_path)

        return output

    @staticmethod
    def template_grab(template_location):
        output = []
        template_location = template_location.replace('.', '/')
        c_path = os.path.join(os.path.dirname(__file__), "..", template_location)
        for existing in os.listdir(c_path):
            existing = str(existing)
            if not existing.endswith(".txt"):
                continue
            output.append(existing.split('.')[0])
        return output

    @staticmethod
    def config_grab():
        # A freshly created world has no config.json yet; return an empty config
        # so the dashboard renders a "not set up" view instead of 500-ing.
        path = DataReader.data_path("config.json")
        if not os.path.exists(path):
            return {}
        with open(path, 'r') as f:
            return json.load(f)

    SCHEDULE_REL = ("cache", "scheduled_attacks.json")

    @staticmethod
    def schedule_path():
        """World-aware path of the queue file the bot reads/writes."""
        return DataReader.data_path(*DataReader.SCHEDULE_REL)

    @staticmethod
    def schedule_grab():
        """World-aware read of the scheduled-attacks queue (shared with the bot,
        which reads the same per-world cache file). Always returns a list."""
        return attack_scheduler.load_schedule(path=DataReader.schedule_path())

    @staticmethod
    def _forced_peace_conflict(arrival_ts):
        """True if arrival_ts (unix seconds) falls inside a configured forced-peace
        window. Mirrors game.village.check_forced_peace: an attack must not arrive
        during forced peace. Windows are naive local-time strings, matching the
        bot's own parsing."""
        config = DataReader.config_grab()
        windows = ((config.get("farms") or {}).get("forced_peace_times")) or []
        arrival = datetime.datetime.fromtimestamp(arrival_ts)
        for pair in windows:
            try:
                start = datetime.datetime.strptime(pair["start"], "%d.%m.%y %H:%M:%S")
                end = datetime.datetime.strptime(pair["end"], "%d.%m.%y %H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            if start <= arrival <= end:
                return True
        return False

    @staticmethod
    def schedule_create(origin_id, target_x, target_y, units, arrival_ts):
        """Queue a timed attack scheduled to LAND at arrival_ts (unix seconds).
        The send moment is back-calculated from the slowest selected unit's
        travel time. Returns (entry, error_message)."""
        origin_id = str(origin_id)
        managed = DataReader.cache_grab("managed")
        origin = managed.get(origin_id) or {}
        pub = origin.get("public") or {}
        loc = pub.get("location")
        if not loc or len(loc) != 2:
            return None, "unknown origin village"
        try:
            tx, ty = int(target_x), int(target_y)
        except (TypeError, ValueError):
            return None, "invalid target coordinates"
        try:
            arrival_ts = int(float(arrival_ts))
        except (TypeError, ValueError):
            return None, "invalid arrival time"

        selected = {}
        for unit, count in (units or {}).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                selected[unit] = count
        if not selected:
            return None, "no units selected"

        if arrival_ts <= int(time.time()):
            return None, "arrival time is in the past"
        if DataReader._forced_peace_conflict(arrival_ts):
            return None, "arrival falls inside a forced-peace window"

        if not (field_distance and unit_travel_seconds):
            return None, "travel-time helpers unavailable"
        ws, us, speeds = DataReader.world_speeds()
        distance = field_distance((loc[0], loc[1]), (tx, ty))
        travels = [unit_travel_seconds(distance, speeds[u], ws, us)
                   for u in selected if speeds.get(u)]
        if not travels:
            return None, "no travel speed for the selected units"
        travel = max(travels)  # the slowest unit dictates arrival

        if arrival_ts - travel <= int(time.time()):
            return None, "troops can't reach the target by that arrival time"

        target_name = None
        for v in DataReader.cache_grab("villages").values():
            vloc = v.get("location")
            if vloc and len(vloc) == 2 and int(vloc[0]) == tx and int(vloc[1]) == ty:
                nm = v.get("name")
                target_name = nm if isinstance(nm, str) and nm else None
                break

        entry = {
            "id": uuid.uuid4().hex[:12],
            "origin_id": origin_id,
            "origin_name": origin.get("name") or pub.get("name") or origin_id,
            "target_x": tx, "target_y": ty,
            "target_name": target_name,
            "units": selected,
            "arrival_ts": arrival_ts,
            "send_ts": int(arrival_ts - travel),
            "travel_seconds": int(travel),
            "distance": round(distance, 1),
            "status": "pending",
            "created": int(time.time()),
        }
        # Append through the shared, locked, atomic store so the bot's concurrent
        # status writes can't clobber this command (and vice versa).
        attack_scheduler.add_command(entry, path=DataReader.schedule_path())
        return entry, None

    @staticmethod
    def schedule_cancel(command_id):
        return attack_scheduler.cancel_command(command_id, path=DataReader.schedule_path())

    CSNIPE_REL = ("cache", "csnipes.json")

    @staticmethod
    def csnipe_path():
        """World-aware path of the c-snipe queue the bot reads/writes."""
        return DataReader.data_path(*DataReader.CSNIPE_REL)

    @staticmethod
    def csnipe_grab():
        """World-aware read of the c-snipe queue. Always returns a list."""
        if csnipe is None:
            return []
        return csnipe.load_snipes(path=DataReader.csnipe_path())

    @staticmethod
    def csnipe_arm(village_id, incoming_id, first_hit_ms, aim_ms, lead_min,
                   target_x, target_y, units, test=False, window_ms=None):
        """Arm a cancel snipe: the bot sends `units` from the attacked village
        toward (target_x|target_y) and cancels at the halfway moment so they
        land back home at first_hit_ms + aim_ms (epoch milliseconds).
        With window_ms > 0 (alpha) the return must land within that many ms
        PAST the target: the engine aims mid-window and re-fires missed sends
        until one lands inside it. 0/None keeps the late-safe one-shot mode.
        With test=True the entry is a dry run against a user-chosen return
        moment instead of an incoming attack - same engine path, just badged.
        Returns (entry, error_message)."""
        if csnipe is None:
            return None, "c-snipe engine unavailable"
        village_id = str(village_id)
        managed = DataReader.cache_grab("managed")
        origin = managed.get(village_id) or {}
        pub = origin.get("public") or {}
        loc = pub.get("location")
        if not loc or len(loc) != 2:
            return None, "unknown village"
        try:
            tx, ty = int(target_x), int(target_y)
            first_hit_ms = int(float(first_hit_ms))
            aim_ms = int(float(aim_ms))
            lead_seconds = int(float(lead_min) * 60)
            window_ms = int(float(window_ms)) if window_ms not in (None, "") else 0
        except (TypeError, ValueError):
            return None, "invalid numbers in the snipe form"
        # Slots repeat every 2s, so a window that wide always hits; cap below it.
        window_ms = max(0, min(1900, window_ms))

        selected = {}
        for unit, count in (units or {}).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                selected[str(unit)] = count
        if not selected:
            return None, "no units selected"

        return_ms = first_hit_ms + aim_ms
        now = time.time()
        if return_ms / 1000.0 - now < 120:
            return None, "too late - the return moment is under 2 minutes away"
        lead_seconds = max(120, min(3600, lead_seconds))

        if not (field_distance and unit_travel_seconds):
            return None, "travel-time helpers unavailable"
        ws, us, speeds = DataReader.world_speeds()
        distance = field_distance((loc[0], loc[1]), (tx, ty))
        travels = [unit_travel_seconds(distance, speeds[u], ws, us)
                   for u in selected if speeds.get(u)]
        if not travels:
            return None, "no travel speed for the selected units"
        # The command travels at its slowest unit's pace and must still be under
        # way at the cancel moment, up to lead/2 after the send (plus margin).
        travel = max(travels)
        if travel < lead_seconds / 2 + 120:
            return None, ("target too close: %s needs a slowest-unit travel "
                          "time over %d min so the troops are still under way "
                          "at the cancel moment" %
                          ("%d|%d" % (tx, ty), (lead_seconds // 2 + 120) // 60))

        target_name = None
        for v in DataReader.cache_grab("villages").values():
            vloc = v.get("location")
            if vloc and len(vloc) == 2 and int(vloc[0]) == tx and int(vloc[1]) == ty:
                nm = v.get("name")
                target_name = nm if isinstance(nm, str) and nm else None
                break

        entry = {
            "id": uuid.uuid4().hex[:12],
            "status": "armed",
            "created": int(now),
            "village_id": village_id,
            "village_name": origin.get("name") or pub.get("name") or village_id,
            "incoming_id": str(incoming_id) if incoming_id else None,
            "first_hit_ms": first_hit_ms,
            "aim_ms": aim_ms,
            "return_ms": return_ms,
            "target_x": tx, "target_y": ty,
            "target_name": target_name,
            "distance": round(distance, 1),
            "units": selected,
            "lead_seconds": lead_seconds,
            "window_ms": window_ms,
            "start_ts": int(max(now, return_ms / 1000.0 - lead_seconds)),
            "test": bool(test),
        }
        csnipe.arm(entry, path=DataReader.csnipe_path())
        return entry, None

    @staticmethod
    def csnipe_disarm(snipe_id):
        """Disarm a snipe (or, when already running, ask the runner to cancel
        the outgoing command right away). Returns the resulting state or None."""
        if csnipe is None:
            return None
        return csnipe.disarm(str(snipe_id), path=DataReader.csnipe_path())

    SNIPE_REL = ("cache", "snipes.json")

    @staticmethod
    def snipe_path():
        """World-aware path of the support-snipe queue the bot executes."""
        return DataReader.data_path(*DataReader.SNIPE_REL)

    @staticmethod
    def snipe_grab():
        """World-aware read of the support-snipe queue. Always returns a list."""
        if snipe_engine is None:
            return []
        return snipe_engine.load_snipes(path=DataReader.snipe_path())

    @staticmethod
    def groups_grab():
        """Cached in-game village groups: [{id, name, type, villages}]."""
        data = (DataReader.cache_grab("world") or {}).get("groups") or {}
        groups = data.get("groups")
        return groups if isinstance(groups, list) else []

    @staticmethod
    def snipe_arm_batch(incoming_id, target_village_id, land_ms, options,
                        shortfall, min_pct, boost):
        """Arm one snipe per selected option: each sends `units` as support
        from its own village so they land at land_ms (epoch milliseconds) in
        the target village. Returns (armed_entries, per_option_errors)."""
        if snipe_engine is None:
            return [], ["snipe engine unavailable"]
        if not (field_distance and unit_travel_seconds):
            return [], ["travel-time helpers unavailable"]
        managed = DataReader.cache_grab("managed")
        target_village_id = str(target_village_id)
        target = managed.get(target_village_id) or {}
        tpub = target.get("public") or {}
        tloc = tpub.get("location")
        if not tloc or len(tloc) != 2:
            return [], ["unknown target village"]
        try:
            land_ms = int(float(land_ms))
            boost = float(boost or 1.0)
            min_pct = max(0, min(100, int(float(
                min_pct if min_pct is not None else snipe_engine.DEFAULT_MIN_PCT))))
        except (TypeError, ValueError):
            return [], ["invalid numbers in the snipe form"]
        if shortfall not in ("scale", "all", "strict"):
            shortfall = "scale"
        if boost <= 0:
            boost = 1.0
        now = time.time()
        if land_ms / 1000.0 - now < 90:
            return [], ["too late - the landing moment is under 90 seconds away"]

        ws, us, speeds = DataReader.world_speeds()
        target_name = target.get("name") or tpub.get("name") or target_village_id
        armed, errors = [], []
        for opt in options or []:
            vid = str((opt or {}).get("village_id"))
            origin = managed.get(vid) or {}
            opub = origin.get("public") or {}
            oloc = opub.get("location")
            oname = origin.get("name") or opub.get("name") or vid
            if not oloc or len(oloc) != 2:
                errors.append("%s: unknown village" % oname)
                continue
            if vid == target_village_id:
                errors.append("%s: a village cannot support itself" % oname)
                continue
            selected = {}
            for unit, count in (opt.get("units") or {}).items():
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    selected[str(unit)] = count
            if not selected:
                errors.append("%s: no units selected" % oname)
                continue
            travels = [unit_travel_seconds(field_distance(oloc, tloc),
                                           speeds[u], ws, us)
                       for u in selected if speeds.get(u)]
            if not travels:
                errors.append("%s: no travel speed for the selected units" % oname)
                continue
            # The command walks at its slowest unit's pace; boost only speeds
            # the *estimate* used for scheduling - the server's own duration
            # (which includes any boost) decides the actual send moment.
            travel = max(travels) / boost
            send_est = land_ms / 1000.0 - travel
            if send_est - now < 30:
                errors.append("%s: the send moment is under 30s away" % oname)
                continue
            entry = {
                "id": uuid.uuid4().hex[:12],
                "status": "armed",
                "created": int(now),
                "incoming_id": str(incoming_id) if incoming_id else None,
                "village_id": vid,
                "village_name": oname,
                "target_village_id": target_village_id,
                "target_name": target_name,
                "target_x": int(tloc[0]), "target_y": int(tloc[1]),
                "units": selected,
                "pace_unit": str(opt.get("pace_unit") or ""),
                "land_ms": land_ms,
                "boost": boost,
                "shortfall": shortfall,
                "min_pct": min_pct,
                "distance": round(field_distance(oloc, tloc), 1),
                "travel_est": int(travel),
                "send_est_ts": int(send_est),
                "start_ts": int(max(now, send_est - snipe_engine.PRESTAGE_SECONDS)),
            }
            snipe_engine.arm(entry, path=DataReader.snipe_path())
            armed.append(entry)
        return armed, errors

    @staticmethod
    def snipe_disarm(snipe_id):
        """Cancel a snipe; before/during the pre-send wait the troops simply
        stay home. Returns the resulting state or None."""
        if snipe_engine is None:
            return None
        return snipe_engine.disarm(str(snipe_id), path=DataReader.snipe_path())

    PLAYERFARM_REL = ("cache", "player_farms.json")

    @staticmethod
    def playerfarm_path():
        """World-aware path of the player-farm hit list the bot reads/writes."""
        return DataReader.data_path(*DataReader.PLAYERFARM_REL)

    @staticmethod
    def playerfarm_grab():
        """World-aware read of the player-farm hit list. Always returns a list."""
        if playerfarm is None:
            return []
        return playerfarm.load_farms(path=DataReader.playerfarm_path())

    @staticmethod
    def playerfarm_add(target_x, target_y, source_id, units, interval_min):
        """Add a village to the player-farm hit list. The target must be known
        in the map cache (that is where its id for report matching comes from).
        Returns (entry, error_message)."""
        if playerfarm is None:
            return None, "player-farm engine unavailable"
        source_id = str(source_id)
        managed = DataReader.cache_grab("managed")
        origin = managed.get(source_id) or {}
        pub = origin.get("public") or {}
        loc = pub.get("location")
        if not loc or len(loc) != 2:
            return None, "unknown source village"
        try:
            tx, ty = int(target_x), int(target_y)
            interval_min = int(float(interval_min))
        except (TypeError, ValueError):
            return None, "invalid numbers in the form"
        interval_min = max(15, min(24 * 60, interval_min))

        selected = {}
        for unit, count in (units or {}).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                selected[str(unit)] = count
        if not selected:
            return None, "no units selected"

        target = None
        for v in DataReader.cache_grab("villages").values():
            vloc = v.get("location")
            if vloc and len(vloc) == 2 and int(vloc[0]) == tx and int(vloc[1]) == ty:
                target = v
                break
        if not target:
            return None, ("no village known at %d|%d yet - it appears in the "
                          "map cache once the bot has seen that area" % (tx, ty))
        if str(target.get("id")) in managed:
            return None, "that village is managed by this account"
        for existing in DataReader.playerfarm_grab():
            if str(existing.get("target_id")) == str(target.get("id")):
                return None, "that village is already on the hit list"

        distance = None
        if field_distance:
            distance = round(field_distance((loc[0], loc[1]), (tx, ty)), 1)

        name = target.get("name")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "created": int(time.time()),
            "status": "active",
            "target_id": str(target.get("id")),
            "target_x": tx, "target_y": ty,
            "target_name": name if isinstance(name, str) and name else "%d|%d" % (tx, ty),
            "target_owner": target.get("owner"),
            "source_id": source_id,
            "source_name": origin.get("name") or pub.get("name") or source_id,
            "units": selected,
            "interval_min": interval_min,
            "distance": distance,
            "last_sent": None,
        }
        playerfarm.add_farm(entry, path=DataReader.playerfarm_path())
        return entry, None

    @staticmethod
    def playerfarm_toggle(farm_id):
        """Pause/resume (or re-enable a stopped) hit-list target."""
        if playerfarm is None:
            return None
        return playerfarm.toggle_farm(str(farm_id),
                                      path=DataReader.playerfarm_path())

    @staticmethod
    def playerfarm_remove(farm_id):
        if playerfarm is None:
            return False
        return playerfarm.remove_farm(str(farm_id),
                                      path=DataReader.playerfarm_path())

    @staticmethod
    def light_carry():
        """The world's light-cavalry haul capacity (cached unit info, or the
        standard 80)."""
        world = DataReader.cache_grab("world")
        carry = ((world.get("unit_info") or {}).get("carry") or {}).get("light")
        return int(carry or getattr(playerfarm, "LIGHT_CARRY_FALLBACK", 80))

    @staticmethod
    def newest_scout_intel(target_id, reports=None):
        """(when, buildings, resources) from the newest report carrying spy
        building data for the village, or (None, None, None)."""
        if reports is None:
            reports = DataReader.cache_grab("reports")
        best = None
        for entry in reports.values():
            if not entry or str(entry.get("dest")) != str(target_id):
                continue
            extra = entry.get("extra") or {}
            if not extra.get("buildings"):
                continue
            when = int(extra.get("when") or 0)
            if best is None or when > best[0]:
                best = (when, extra["buildings"], extra.get("resources"))
        return best or (None, None, None)

    @staticmethod
    def playerfarm_estimate(target_x, target_y, interval_min, levels=None):
        """Production estimate for the farm calculator. With `levels` (manual
        {wood, stone, iron[, storage]} mine levels) it computes directly;
        otherwise it looks up the newest scout intel for the village at the
        coords. Returns (estimate_dict, error_message)."""
        if playerfarm is None:
            return None, "player-farm engine unavailable"
        try:
            interval = int(float(interval_min or 60))
        except (TypeError, ValueError):
            return None, "invalid interval"
        world_speed, _us, _speeds = DataReader.world_speeds()
        carry = DataReader.light_carry()

        buildings, resources, when = levels, None, None
        if not levels:
            try:
                tx, ty = int(target_x), int(target_y)
            except (TypeError, ValueError):
                return None, "invalid coordinates"
            village = None
            for v in DataReader.cache_grab("villages").values():
                loc = v.get("location")
                if loc and len(loc) == 2 and int(loc[0]) == tx and int(loc[1]) == ty:
                    village = v
                    break
            if village:
                when, buildings, resources = DataReader.newest_scout_intel(
                    village.get("id"))
            if not buildings:
                return {"found": False, "carry": carry}, None

        eco = playerfarm.estimate_economy(buildings, resources, when, world_speed)
        eco["found"] = True
        eco["carry"] = carry
        eco["levels"] = {k: int(buildings.get(k) or 0)
                         for k in ("wood", "stone", "iron")}
        eco["storage_level"] = buildings.get("storage")
        eco["suggested_light"] = playerfarm.suggest_light(
            eco["hourly_total"], interval, carry)
        return eco, None

    @staticmethod
    def example_village_template():
        """Default village_template from config.example.json. Used to backfill
        settings keys added after a world's config.json was first written (e.g.
        new scavenge options) so they still render in the dashboard before the
        bot's next config merge persists them."""
        path = os.path.join(os.path.dirname(__file__), "..", "config.example.json")
        try:
            with open(path, "r") as f:
                return (json.load(f) or {}).get("village_template", {}) or {}
        except Exception:
            return {}

    @staticmethod
    def config_set(parameter, value):
        try:
            value = json.loads(value)
        except:
            pass
        config_file_path = DataReader.data_path("config.json")
        with open(config_file_path, 'r') as config_file:
            template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
            if "." in parameter:
                section, param = parameter.split('.')
                template[section][param] = value
            else:
                template[parameter] = value
            with open(config_file_path, 'w') as newcf:
                json.dump(template, newcf, indent=2, sort_keys=False)
                print("Deployed new configuration file")
                return True

    @staticmethod
    def village_config_set(village_id, parameter, value):
        config_file_path = DataReader.data_path("config.json")
        with open(config_file_path, 'r') as config_file:
            template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
            if village_id not in template['villages']:
                return False
            try:
                template['villages'][str(village_id)][parameter] = json.loads(value)
            except json.decoder.JSONDecodeError:
                template['villages'][str(village_id)][parameter] = value
            with open(config_file_path, 'w') as newcf:
                json.dump(template, newcf, indent=2, sort_keys=False)
                print("Deployed new configuration file")
                return True

    @staticmethod
    def incoming_tag_set(command_id, tag):
        """Persist a user-set tag onto a cached incoming command."""
        command_id = os.path.basename(str(command_id))
        path = DataReader.data_path("cache", "incomings", "%s.json" % command_id)
        if not os.path.exists(path):
            return False
        with open(path, 'r') as f:
            entry = json.load(f)
        entry['tag'] = tag or None
        with open(path, 'w') as f:
            json.dump(entry, f, indent=2, sort_keys=False)
        return True

    @staticmethod
    def incoming_rename_ingame(command_id, label):
        """Push a tag to TribalWars as the incoming attack's in-game label.

        Reuses the bot's saved session cookies and the rename endpoint captured
        from a live incomings page. Returns a status dict; in-game renaming stays
        inactive (reason 'no_endpoint_yet') until the bot has scraped the
        incomings page at least once while logged in.
        """
        if not rename_command_ingame:
            return {"ok": False, "reason": "unavailable"}
        command_id = os.path.basename(str(command_id))
        session = DataReader.get_session()
        cookies = (session or {}).get("cookies") or {}
        endpoint = (session or {}).get("endpoint") or ""
        user_agent = None
        try:
            cfg_path = DataReader.data_path("config.json")
            with open(cfg_path, 'r') as cf:
                user_agent = json.load(cf).get("bot", {}).get("user_agent")
        except (OSError, ValueError):
            pass
        return rename_command_ingame(command_id, label, cookies, endpoint, user_agent)

    @staticmethod
    def apply_village_template(village_id=None):
        """
        Copy the default `village_template` settings onto an existing village (or all
        villages when village_id is None). Overwrites the per-village values with the
        defaults so a village can be reset to the template behaviour in one click.
        Returns the number of villages updated, or False if there is no template.
        """
        config_file_path = DataReader.data_path("config.json")
        with open(config_file_path, 'r') as config_file:
            template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
        defaults = template.get("village_template", {})
        if not defaults:
            return False
        villages = template.get("villages", {})
        targets = [str(village_id)] if village_id is not None else list(villages.keys())
        applied = 0
        for vid in targets:
            if vid not in villages:
                continue
            for key, value in defaults.items():
                villages[vid][key] = value
            applied += 1
        with open(config_file_path, 'w') as newcf:
            json.dump(template, newcf, indent=2, sort_keys=False)
        return applied

    @staticmethod
    def apply_opening_strategy(village_id):
        """Apply the day 1-5 'opening (into off)' preset to one village: the
        spear-rush build + troop templates, scavenging on, and population-priority
        farming. A one-time set-up for a world's first village - the templates
        flow into the full game, so nothing needs switching off later. Returns
        False if the village has no config entry.
        """
        preset = {
            "building": "opening_into_off",
            "units": "opening_into_off",
            "gather_enabled": True,
            "advanced_gather": True,
            "gather_selection": 4,
            "scavenge_unlock_enabled": True,
            "farm_priority_pop_pct": 80,
        }
        config_file_path = DataReader.data_path("config.json")
        with open(config_file_path, 'r') as config_file:
            template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
        villages = template.get("villages", {})
        vid = str(village_id)
        if vid not in villages:
            return False
        villages[vid].update(preset)
        with open(config_file_path, 'w') as newcf:
            json.dump(template, newcf, indent=2, sort_keys=False)
        return True

    @staticmethod
    def broadcast_village_set(parameter, value):
        """
        Set a per-village parameter on every village plus the village_template,
        so a single quick-toggle (e.g. scavenging) applies account-wide.
        """
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
        config_file_path = DataReader.data_path("config.json")
        with open(config_file_path, 'r') as config_file:
            template = json.load(config_file, object_pairs_hook=collections.OrderedDict)
        for vid in template.get("villages", {}):
            template["villages"][vid][parameter] = value
        if "village_template" in template:
            template["village_template"][parameter] = value
        with open(config_file_path, 'w') as newcf:
            json.dump(template, newcf, indent=2, sort_keys=False)
        return True

    @staticmethod
    def get_session():
        c_path = DataReader.data_path("cache", "session.json")
        if not os.path.exists(c_path):
            return {"raw": "", "endpoint": "None", "server": "None", "world": "None"}
        with open(c_path, 'r') as session_file:
            session_data = json.load(session_file)
            cookies = []
            for c in session_data['cookies']:
                cookies.append("%s=%s" % (c, session_data['cookies'][c]))
            session_data['raw'] = ';'.join(cookies)
            return session_data

    @staticmethod
    def parse_cookie_string(raw):
        """
        Parse a raw browser cookie string ("k=v; k2=v2") into a dict, using the
        same splitting rules as core/request.py so the bot reads it identically.
        """
        cookies = {}
        cleaned = (raw or "").strip().replace('\n', '').replace('\r', '')
        for item in cleaned.split(';'):
            item = item.strip()
            if not item or '=' not in item:
                continue
            kvs = item.split('=')
            key = kvs[0].strip()
            value = '='.join(kvs[1:])
            if key:
                cookies[key] = value
        return cookies

    @staticmethod
    def session_set(raw):
        """
        Save a pasted cookie string as the active session.

        Writes both cache/session.json (which the bot checks first) and
        cache/cookies.txt (its fallback), so a paste from the dashboard replaces
        the need to hand-edit cookies.txt. Endpoint/server are preserved from the
        existing session, falling back to config.json. Returns False if the string
        contains no usable cookies.
        """
        cookies = DataReader.parse_cookie_string(raw)
        if not cookies:
            return False

        cache_dir = DataReader.data_path("cache")
        session_path = os.path.join(cache_dir, "session.json")

        endpoint, server = None, None
        if os.path.exists(session_path):
            try:
                with open(session_path, 'r') as existing:
                    prev = json.load(existing)
                    endpoint = prev.get("endpoint")
                    server = prev.get("server")
            except (ValueError, OSError):
                pass
        if not endpoint or not server:
            try:
                config = DataReader.config_grab()
                endpoint = endpoint or config.get("server", {}).get("endpoint")
                server = server or config.get("server", {}).get("server")
            except (ValueError, OSError):
                pass

        with open(session_path, 'w') as session_file:
            json.dump({"endpoint": endpoint, "server": server, "cookies": cookies},
                      session_file, indent=2)
        # Keep the raw fallback in sync with what was just pasted.
        with open(os.path.join(cache_dir, "cookies.txt"), 'w') as cookie_file:
            cookie_file.write((raw or "").strip())

        # Optimistically clear the incoming poller's logged-out flag so the
        # dashboard banner reflects the fresh cookie right away instead of
        # waiting for the next successful poll. If the new cookie is also dead
        # the poller re-sets the flag on its next failed scrape. notified_at is
        # preserved so re-notification throttling carries over.
        state_path = DataReader.data_path("cache", "world", "incoming_session.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, 'r') as state_file:
                    state = json.load(state_file) or {}
            except (ValueError, OSError):
                state = {}
            state["logged_out"] = False
            try:
                with open(state_path, 'w') as state_file:
                    json.dump(state, state_file, indent=2)
            except OSError:
                pass
        return True

    @staticmethod
    def portal_cookies_set(raw):
        cookies = DataReader.parse_cookie_string(raw)
        if not cookies:
            return False
        cache_dir = DataReader.data_path("cache")
        with open(os.path.join(cache_dir, "portal_cookies.json"), 'w') as f:
            json.dump({"domain": "www.tribalwars.nl", "cookies": cookies}, f, indent=2)
        return True

    @staticmethod
    def portal_cookies_get():
        p = DataReader.data_path("cache", "portal_cookies.json")
        if not os.path.exists(p):
            # Portal cookies are account-level (www.tribalwars.nl), not
            # world-level — fall back to the newest copy from any world.
            candidates = glob.glob(os.path.join(
                DataReader.project_root(), "worlds", "*", "cache", "portal_cookies.json"))
            if not candidates:
                return {}
            p = max(candidates, key=os.path.getmtime)
        try:
            with open(p) as f:
                return json.load(f).get("cookies", {})
        except (ValueError, OSError):
            return {}


class BuildingTemplateManager:

    @staticmethod
    def template_cache_list():
        c_path = os.path.join(os.path.dirname(__file__), "..", "templates", "builder")
        output = {}
        for existing in os.listdir(c_path):
            if not existing.endswith(".txt"):
                continue
            with open(os.path.join(os.path.dirname(__file__), "..", "templates", "builder", existing),
                      'r') as template_file:
                output[existing] = BuildingTemplateManager.template_to_dict(
                    [x.strip() for x in template_file.readlines()])
        return output

    @staticmethod
    def template_dir():
        return os.path.join(os.path.dirname(__file__), "..", "templates", "builder")

    @staticmethod
    def _template_path(name):
        name = os.path.basename(name)
        if not name.endswith(".txt"):
            name = "%s.txt" % name
        return os.path.join(BuildingTemplateManager.template_dir(), name)

    @staticmethod
    def template_save(name, rows):
        """
        Write an ordered building template. `rows` is a list of (building, level);
        each becomes a `building:level` line, in order, matching the format the bot's
        builder reads (whitespace-separated building:level tokens, no comments).
        """
        path = BuildingTemplateManager._template_path(name)
        lines = ["%s:%d" % (building, level) for building, level in rows]
        with open(path, "w") as template_file:
            template_file.write("\n".join(lines))
            if lines:
                template_file.write("\n")
        return True

    @staticmethod
    def template_delete(name):
        path = BuildingTemplateManager._template_path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    @staticmethod
    def template_to_dict(t_list):
        out_data = {}
        rows = []

        for entry in t_list:
            if entry.startswith('#') or ':' not in entry:
                continue
            building, next_level = entry.split(':')
            next_level = int(next_level)
            old = 0
            if building in out_data:
                old = out_data[building]
            rows.append({'building': building, 'from': old, 'to': next_level})
            out_data[building] = next_level

        return rows


class UnitTemplateManager:
    """File IO for the JSON troop templates in templates/troops/."""

    @staticmethod
    def template_dir():
        return os.path.join(os.path.dirname(__file__), "..", "templates", "troops")

    @staticmethod
    def _template_path(name):
        name = os.path.basename(name)
        if not name.endswith(".txt"):
            name = "%s.txt" % name
        return os.path.join(UnitTemplateManager.template_dir(), name)

    @staticmethod
    def template_cache_list():
        out = {}
        directory = UnitTemplateManager.template_dir()
        for existing in sorted(os.listdir(directory)):
            if not existing.endswith(".txt"):
                continue
            try:
                with open(os.path.join(directory, existing), "r") as template_file:
                    data = json.load(template_file)
            except (ValueError, OSError):
                data = []
            out[existing] = data if isinstance(data, list) else []
        return out

    @staticmethod
    def template_get(name):
        path = UnitTemplateManager._template_path(name)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r") as template_file:
                data = json.load(template_file)
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def template_save(name, stages):
        """Write a troop template (list of stage dicts) as pretty JSON."""
        path = UnitTemplateManager._template_path(name)
        with open(path, "w") as template_file:
            json.dump(stages, template_file, indent=2, sort_keys=False)
            template_file.write("\n")
        return True

    @staticmethod
    def template_delete(name):
        path = UnitTemplateManager._template_path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False


class MapBuilder:

    @staticmethod
    def build(villages, current_village=None, size=None):
        out_map = {}
        min_x = 999
        max_x = 0
        min_y = 999
        max_y = 0

        current_location = None
        grid_vils = {}
        extra_data = {}

        for v in villages:
            vdata = villages[v]
            x, y = vdata['location']
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x

            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            if current_village and vdata['id'] == current_village:
                current_location = vdata['location']
                extra_data['owner'] = vdata['owner']
                extra_data['tribe'] = vdata['tribe']
            grid_vils["%d:%d" % (x, y)] = vdata

        if current_location and size:
            min_x = current_location[0] - size
            min_y = current_location[1] - size
            max_x = current_location[0] + size
            max_y = current_location[1] + size

        for location_x in range(min_x, max_x):
            if location_x not in out_map:
                out_map[location_x - min_x] = {}
            ylocs = {}
            for location_y in range(min_y, max_y):
                location = "%d:%d" % (location_x, location_y)
                if location in grid_vils:
                    ylocs[location_y - min_y] = grid_vils[location]
                else:
                    ylocs[location_y - min_y] = None
            out_map[location_x - min_x] = ylocs

        return {"grid": out_map, "extra": extra_data}


class OverviewBuilder:
    """
    Turns the raw cache dumps into an at-a-glance summary for the status page:
    aggregate totals, a per-village snapshot and a recent-activity feed.
    """

    # Report types that represent real bot activity worth showing in the feed.
    ACTIVITY_TYPES = ("attack", "scout")

    # Silent-stall detector: if farming is on and it's active hours but no
    # attack/scout report has been ingested for this long while the main loop is
    # still turning, flag a likely stall (e.g. a degraded session that stopped
    # report reading). Farm runs happen every ~25-45 min and scouts return within
    # a couple of hours, so several hours of nothing is anomalous.
    FARM_STALL_SECONDS = 3 * 3600

    # Memo for the full-report scan used by the 24h/all-time counters: a tuple of
    # (signature, compact_records). The signature is a cheap fingerprint of the
    # reports dir, so the O(all reports) JSON parse only reruns when a report is
    # actually added/changed - not on every dashboard refresh.
    _reports_memo = None

    @staticmethod
    def _reports_signature():
        """Cheap fingerprint of the active world's reports dir: (path, file_count,
        newest_mtime). Statting the files is far cheaper than parsing them, and
        the path keeps the memo correct across a world switch."""
        rpath = DataReader.data_path("cache", "reports")
        if not os.path.isdir(rpath):
            return (rpath, 0, 0.0)
        count = 0
        newest = 0.0
        try:
            with os.scandir(rpath) as it:
                for e in it:
                    if not e.name.endswith(".json"):
                        continue
                    count += 1
                    try:
                        m = e.stat().st_mtime
                    except OSError:
                        continue
                    if m > newest:
                        newest = m
        except OSError:
            return (rpath, 0, 0.0)
        return (rpath, count, newest)

    @classmethod
    def _farm_trade_records(cls):
        """Compact (type, when, loot_sum) tuples for every report on disk, memoised
        by _reports_signature so the full JSON parse only reruns when the reports
        change. The time-windowed counters are summed from these in memory against
        the current cutoff each build (cheap), keeping 24h totals second-accurate."""
        sig = cls._reports_signature()
        memo = cls._reports_memo
        if memo is not None and memo[0] == sig:
            return memo[1]
        try:
            all_reports = DataReader.cache_grab("reports") or {}
        except Exception:
            all_reports = {}
        records = []
        for r in all_reports.values():
            rtype = (r or {}).get("type")
            ex = (r or {}).get("extra", {}) or {}
            when = cls._to_int(ex.get("when"))
            loot_sum = 0
            if rtype == "attack":
                loot_sum = sum(cls._to_int(v) for v in (ex.get("loot", {}) or {}).values())
            records.append((rtype, when, loot_sum))
        cls._reports_memo = (sig, records)
        return records

    @staticmethod
    def _in_active_hours(spec):
        """True if the current local hour falls in the bot's active_hours window.
        Mirrors twb.py is_active_hours (end-inclusive, handles overnight wrap).
        Unset/malformed -> treated as always active."""
        if not spec:
            return True
        try:
            start, end = [int(h) for h in str(spec).split("-")]
        except (ValueError, TypeError):
            return True
        h = time.localtime().tm_hour
        if start <= end:
            return start <= h <= end
        return h >= start or h <= end

    @classmethod
    def _farm_stall_state(cls, newest_combat_ts, watchdog):
        """Detect the 'silent stall': the main loop is alive during active hours
        and farming is on, but no attack/scout report has been ingested for
        FARM_STALL_SECONDS. This is the nl99 signature - a degraded session that
        keeps the loop turning (fresh heartbeat) while report reading is dead.
        Returns {"stalled": bool, "since": ts, "age": secs}."""
        idle = {"stalled": False, "since": None, "age": None}
        # A captcha/heartbeat stall is already surfaced as critical; don't stack.
        if watchdog.get("stalled"):
            return idle
        try:
            cfg = DataReader.config_grab() or {}
        except Exception:
            return idle
        # Only meaningful when farming is on (that's what produces these reports)...
        if not (cfg.get("farms", {}) or {}).get("farm"):
            return idle
        # ...and during active hours, when the bot should be actively farming.
        if not cls._in_active_hours((cfg.get("bot", {}) or {}).get("active_hours")):
            return idle
        # No baseline yet (fresh world, no attack/scout reports) -> don't cry wolf.
        if not newest_combat_ts:
            return idle
        age = int(time.time()) - int(newest_combat_ts)
        if age > cls.FARM_STALL_SECONDS:
            return {"stalled": True, "since": int(newest_combat_ts), "age": age}
        return idle

    @staticmethod
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _build_incomings(cls, village_db):
        """Group tracked incoming commands by target village, enriched with
        per-unit walking times, an auto-tag suggestion and enemy village info.
        """
        incomings = DataReader.cache_grab("incomings")
        by_target = {}
        if not incomings:
            return by_target

        # World-aware speeds (load_world_speeds goes through FileManager, which
        # isn't world-aware in the web process).
        world_speed, unit_speed, speeds = DataReader.world_speeds()
        now = int(time.time())

        for command_id, entry in incomings.items():
            arrival = entry.get("arrival")
            first_seen = entry.get("first_seen") or now
            distance = entry.get("distance")

            units = []
            tag_auto = None
            if speeds and distance and arrival and slowest_floor:
                table = travel_table(distance, speeds, world_speed, unit_speed)
                # The attack has been in the air at least (arrival - first_seen),
                # so its slowest unit is at least that slow. tag_auto is the
                # fastest unit still consistent with that - the tightest estimate.
                remaining_detect = arrival - first_seen
                tag_auto = slowest_floor(table, remaining_detect)
                for unit in UNIT_ORDER:
                    if unit not in table:
                        continue
                    secs = table[unit]
                    units.append({
                        "unit": unit,
                        "seconds": int(secs),
                        # A unit can be the slowest (tag) unit only if its trip is
                        # at least as long as the time we've seen it flying;
                        # anything faster would already have landed.
                        "possible": secs >= remaining_detect,
                    })

            enemy = village_db.get(str(entry.get("origin_id"))) or {}
            view = {
                "command_id": command_id,
                "origin_name": entry.get("origin_name"),
                "origin_id": entry.get("origin_id"),
                "origin_coords": entry.get("origin_coords"),
                "player_name": entry.get("player_name"),
                "player_id": entry.get("player_id"),
                "distance": distance,
                "arrival": arrival,
                "arrival_ms": entry.get("arrival_ms"),
                "first_seen": first_seen,
                "eta": (arrival - now) if arrival else None,
                "game_label": entry.get("game_label"),
                "tag": entry.get("tag"),
                "tag_auto": tag_auto,
                "units": units,
                "enemy_points": enemy.get("points"),
                "enemy_tribe": enemy.get("tribe"),
                "enemy_owner": enemy.get("owner"),
            }
            by_target.setdefault(str(entry.get("target_id")), []).append(view)

        for commands in by_target.values():
            commands.sort(key=lambda c: c.get("arrival") or 0)
        return by_target

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        attacks = data.get("attacks", {}) or {}
        reports = data.get("reports", {}) or {}
        incomings_by_target = cls._build_incomings(data.get("villages", {}) or {})

        # Whether the incoming poller is currently logged out (cookie expired).
        # When true the incomings panel is blind, so the dashboard must show that
        # explicitly instead of an empty "all clear". Read via DataReader so it
        # resolves to the active world's dir (incoming_session_state() uses the
        # bot's FileManager, which isn't world-selected in the web context).
        incoming_logged_out = bool(DataReader.session_logged_out())

        # The live incoming cache is authoritative only when the poller is active
        # and logged in. Otherwise (poller disabled, or logged out and blind) we
        # fall back to the village's coarse under_attack flag.
        try:
            poller_enabled = bool(DataReader.config_grab().get("bot", {}).get("incoming_check", True))
        except Exception:
            poller_enabled = True
        trust_live_incomings = poller_enabled and not incoming_logged_out

        villages = []
        totals = {"wood": 0, "stone": 0, "iron": 0, "pop": 0}
        total_troops = {}  # all troops owned (incl. away or in training)
        home_troops = {}   # troops currently sitting in a village
        queued_total = 0
        active_incoming = []  # villages the bot currently sees as under attack
        managed_ids = set(str(v) for v in managed.keys())

        for vid, vdata in managed.items():
            public = vdata.get("public", {}) or {}
            resources = vdata.get("resources", {}) or {}
            available = vdata.get("available_troops", {}) or {}
            owned = vdata.get("troops", {}) or {}
            queue = vdata.get("building_queue", []) or []

            for key in ("wood", "stone", "iron", "pop"):
                totals[key] += cls._to_int(resources.get(key))
            for unit, amount in available.items():
                home_troops[unit] = home_troops.get(unit, 0) + cls._to_int(amount)
            for unit, amount in owned.items():
                total_troops[unit] = total_troops.get(unit, 0) + cls._to_int(amount)
            queued_total += len(queue)

            name = vdata.get("name") or public.get("name") or vid
            # Drive the "under attack now" card off the live, pruned incoming cache
            # (refreshed every few minutes by the poller) rather than the village's
            # under_attack flag, which only updates on a full village run and so
            # stays stuck "true" after the attacks have already landed. Only fall
            # back to that flag when the live cache isn't trustworthy (poller
            # disabled or logged out); when it is, zero incomings means zero.
            cmds = incomings_by_target.get(str(vid), [])
            future = [c for c in cmds if (c.get("eta") or 0) > 0]
            if future or (vdata.get("under_attack") and not trust_live_incomings):
                active_incoming.append({
                    "id": vid,
                    "name": name,
                    "commands": future,
                })

            villages.append({
                "id": vid,
                "name": name,
                "points": public.get("points"),
                "location": public.get("location"),
                "resources": resources,
                "troops": {u: a for u, a in available.items() if cls._to_int(a) > 0},
                "queue_len": len(queue),
                # First few planned builds, rendered as chips with mouseover.
                "queue_preview": [parse_queue_entry(e) for e in queue[:5]],
            })

        villages.sort(key=lambda v: cls._to_int(v.get("points")), reverse=True)

        # Farm / attack target tracking (cache/attacks: target -> {kind, last_attack})
        scout_targets = sum(1 for a in attacks.values() if a.get("kind") == "scout")
        farm_targets = len(attacks) - scout_targets

        # Activity feed from attack/scout reports, newest first.
        activity = []
        scavenging_runs = 0
        trades = 0
        for rid, report in reports.items():
            rtype = report.get("type")
            if rtype == "ScavengingCompletedReport":
                scavenging_runs += 1
                continue
            if rtype == "ReportTrade":
                trades += 1
                continue
            if rtype not in cls.ACTIVITY_TYPES:
                continue
            extra = report.get("extra", {}) or {}
            loot = extra.get("loot", {}) or {}
            # Target wall level from a scouted buildings snapshot. A buildings dict
            # with no "wall" key means it was scouted and there is no wall (level 0);
            # no buildings dict at all means we never saw it (unknown -> None).
            buildings = extra.get("buildings") or {}
            wall = cls._to_int(buildings.get("wall", 0)) if buildings else None
            activity.append({
                "id": rid,
                "type": rtype,
                "origin": report.get("origin"),
                "dest": report.get("dest"),
                "when": extra.get("when", 0),
                "loot_total": sum(cls._to_int(v) for v in loot.values()),
                "loot": loot,
                "wall": wall,
                "units_sent": extra.get("units_sent", {}) or {},
                "losses": extra.get("units_losses", {}) or {},
                # Incoming = someone acting on us (a known origin that isn't ours)
                "incoming": bool(report.get("origin")) and str(report.get("origin")) not in managed_ids,
            })
        activity.sort(key=lambda a: a.get("when") or 0, reverse=True)
        # Past incoming hits (from reports) - shown calmly as history, distinct from
        # active_incoming above which is the live "under attack right now" signal.
        recent_incoming = [a for a in activity if a["incoming"]][:10]

        # Recent farm haul + latest activity time, over the same window of reports
        # as the scavenging/trade counters above (sync() keeps the newest ~100).
        loot_recent = sum(
            a["loot_total"] for a in activity
            if a["type"] == "attack" and not a["incoming"]
        )
        last_activity = max((a["when"] for a in activity), default=0)

        # How many managed villages have scavenging switched on (from config), for
        # the overview Scavenging card.
        try:
            cfg_villages = (DataReader.config_grab().get("villages", {}) or {})
        except Exception:
            cfg_villages = {}
        scavenging_enabled = sum(1 for v in cfg_villages.values() if v.get("gather_enabled"))

        # Troops away from home (supporting other villages, attacking, or in transit)
        # = total owned minus what is currently sitting at home, per unit.
        troops_away = {u: max(0, total_troops.get(u, 0) - home_troops.get(u, 0))
                       for u in total_troops}
        # Split "away" into in-transit ("op pad") and support (stationed in other
        # villages). Both are read straight from the game and cached by the bot
        # (cache/troops_moving.json); we do NOT derive support by subtraction
        # because the snapshots are taken at different moments and that leaves
        # phantom troops. If the cache is missing, fall back to lumped "away".
        cache = {}
        try:
            mpath = DataReader.data_path("cache", "troops_moving.json")
            if os.path.exists(mpath):
                with open(mpath) as f:
                    cache = json.load(f) or {}
        except Exception:
            cache = {}
        has_cache = isinstance(cache, dict) and ("moving" in cache or "support" in cache)
        troops_moving = {u: cls._to_int(c) for u, c in (cache.get("moving", {}) or {}).items() if cls._to_int(c)}
        if has_cache:
            troops_support = {u: cls._to_int(c) for u, c in (cache.get("support", {}) or {}).items() if cls._to_int(c)}
        else:
            troops_support = troops_away  # no live split yet: show the lumped total

        # Last-24h and all-time counters over the FULL report cache (sync() only
        # passes the newest ~100, which is enough for the feed but not for totals).
        # A farm run = an attack report that returned loot. The full parse is
        # memoised (_farm_trade_records) so it only reruns when reports change;
        # the windowed sums below are cheap in-memory arithmetic against `cutoff`.
        cutoff = int(time.time()) - 86400
        farm_runs_total = farm_runs_24h = 0
        farm_loot_total = farm_loot_24h = 0
        scav_runs_total = scav_runs_24h = 0
        trades_total = trades_24h = 0
        newest_combat = 0  # newest attack/scout report time, for the stall detector
        for rtype, when, loot_sum in cls._farm_trade_records():
            recent = when and when >= cutoff
            if rtype in cls.ACTIVITY_TYPES and when and when > newest_combat:
                newest_combat = when
            if rtype == "attack":
                if loot_sum > 0:  # a farm haul
                    farm_runs_total += 1
                    farm_loot_total += loot_sum
                    if recent:
                        farm_runs_24h += 1
                        farm_loot_24h += loot_sum
            elif rtype == "ReportTrade":
                trades_total += 1
                if recent:
                    trades_24h += 1

        # Real in-game HQ build queue (how many buildings are actually queued
        # now, across all villages) — not the bot's planned build order.
        active_build_items = sum(
            cls._to_int((vd or {}).get("active_building_queue", 0))
            for vd in managed.values()
        )

        # Scavenging loot. The completed reports carry no haul, so the bot logs
        # each run's expected loot (carry capacity * option ratio) to
        # cache/scavenge_log.json: an all-time total plus ~25h of runs for 24h.
        scav_log = {}
        try:
            scav_path = DataReader.data_path("cache", "scavenge_log.json")
            if os.path.exists(scav_path):
                with open(scav_path) as f:
                    scav_log = json.load(f) or {}
        except Exception:
            scav_log = {}
        # Scavenging runs + loot both come from the dispatch log so they stay
        # consistent: a run is counted when it's sent (it produces no completed
        # report until hours later, and those reports carry no haul anyway).
        scav_runs = scav_log.get("runs", []) or []
        recent_runs = [r for r in scav_runs if cls._to_int(r.get("when")) >= cutoff]
        scav_loot_total = cls._to_int(scav_log.get("total_loot"))
        scav_loot_24h = sum(cls._to_int(r.get("loot")) for r in recent_runs)
        scav_runs_total = cls._to_int(scav_log.get("total_runs")) or len(scav_runs)
        scav_runs_24h = len(recent_runs)

        _watchdog = DataReader.watchdog_state()
        _watchdog["farm_stall"] = cls._farm_stall_state(newest_combat, _watchdog)

        return {
            "summary": {
                "villages": len(managed),
                "farm_targets": farm_targets,
                "scout_targets": scout_targets,
                "scavenging_runs": scavenging_runs,
                "scavenging_enabled": scavenging_enabled,
                "trades": trades,
                "queued_buildings": queued_total,
                "active_build_items": active_build_items,
                "farm_runs_24h": farm_runs_24h,
                "farm_runs_total": farm_runs_total,
                "farm_loot_24h": farm_loot_24h,
                "farm_loot_total": farm_loot_total,
                "scav_runs_24h": scav_runs_24h,
                "scav_runs_total": scav_runs_total,
                "scav_loot_24h": scav_loot_24h,
                "scav_loot_total": scav_loot_total,
                "trades_24h": trades_24h,
                "trades_total": trades_total,
                "resources": totals,
                "troops": total_troops,
                "troops_home": home_troops,
                "troops_away": troops_away,
                "troops_support": troops_support,
                "troops_moving": troops_moving,
                "loot_recent": loot_recent,
                "last_activity": last_activity,
                "watchdog": _watchdog,
            },
            "villages": villages,
            "activity": activity[:20],
            "active_incoming": active_incoming,
            # Total individual attacks across all villages (not village count), for
            # the card header. Falls back to village count when no detail is known.
            "incoming_total": sum(len(v["commands"]) for v in active_incoming),
            "recent_incoming": recent_incoming,
            "incoming_logged_out": incoming_logged_out,
        }


class AttackPlanner:
    """Data for the attack-planner page.

    Surfaces our own villages (possible origins), the targets the bot is already
    tracking (cache/attacks, enriched from the village DB) and the world's unit
    speeds, so the page can compute per-unit travel/arrival times client-side.
    """

    @staticmethod
    def build(data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}
        attacks = data.get("attacks", {}) or {}

        origins = []
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            origins.append({
                "id": vid,
                "name": vdata.get("name") or pub.get("name") or vid,
                "coords": pub.get("location"),
                "points": pub.get("points"),
                # Troops standing in the village right now, for the schedule form.
                "troops": {u: int(n) for u, n in (vdata.get("available_troops") or {}).items()},
            })
        origins.sort(key=lambda o: str(o["name"]))

        targets = []
        for tid, info in attacks.items():
            v = village_db.get(str(tid)) or {}
            name = v.get("name")
            targets.append({
                "id": tid,
                "name": name if isinstance(name, str) and name else None,
                "coords": v.get("location"),
                "points": v.get("points"),
                "owner": v.get("owner"),
                "tribe": v.get("tribe"),
                "kind": info.get("kind"),
                "last_attack": info.get("last_attack"),
            })
        targets.sort(key=lambda t: t.get("last_attack") or 0, reverse=True)

        world_speed, unit_speed, speeds = DataReader.world_speeds()
        units = [u for u in UNIT_ORDER if u in speeds]

        return {
            "origins": origins,
            "targets": targets,
            "units": units,
            # base minutes-per-field per unit + world multipliers, for the JS calc
            "speeds": {u: speeds[u] for u in units},
            "world_speed": world_speed,
            "unit_speed": unit_speed,
            "now": int(time.time()),
        }


class DefenseOverview:
    """Per-village defensive picture: who is under attack now (live incomings),
    the defensive troops sitting at home, and account-wide totals.
    """

    DEFENSIVE_UNITS = ["spear", "sword", "archer", "marcher", "spy"]

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}
        incomings_by_target = OverviewBuilder._build_incomings(village_db)
        now = int(time.time())

        villages = []
        total_def = {u: 0 for u in cls.DEFENSIVE_UNITS}
        under_attack_count = 0
        total_incoming = 0

        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            avail = vdata.get("available_troops", {}) or {}
            home_def = {u: OverviewBuilder._to_int(avail.get(u)) for u in cls.DEFENSIVE_UNITS}
            for u in cls.DEFENSIVE_UNITS:
                total_def[u] += home_def[u]

            cmds = incomings_by_target.get(str(vid), [])
            future = [c for c in cmds if (c.get("eta") or 0) > 0]
            soonest = min((c["eta"] for c in future), default=None)
            if future:
                under_attack_count += 1
                total_incoming += len(future)

            villages.append({
                "id": vid,
                "name": vdata.get("name") or pub.get("name") or vid,
                "coords": pub.get("location"),
                "points": pub.get("points"),
                "incoming": len(future),
                "soonest_eta": soonest,
                "soonest_arrival": (now + soonest) if soonest is not None else None,
                "commands": future,
                "def_troops": home_def,
                "def_total": sum(home_def.values()),
            })

        # Under-attack villages first (soonest arrival first), then strongest garrisons.
        villages.sort(key=lambda v: (
            0 if v["incoming"] else 1,
            v["soonest_eta"] if v["soonest_eta"] is not None else 1 << 62,
            -v["def_total"],
        ))

        return {
            "villages": villages,
            "defensive_units": cls.DEFENSIVE_UNITS,
            "total_def": total_def,
            "total_def_sum": sum(total_def.values()),
            "under_attack_count": under_attack_count,
            "total_incoming": total_incoming,
            "village_count": len(managed),
        }


def live_incomings(managed, village_db):
    """Future incoming attacks as dashboard rows, soonest arrival first
    (shared by the C-snipe and Snipe tabs)."""
    incomings_by_target = OverviewBuilder._build_incomings(village_db)
    incomings = []
    for vid, vdata in managed.items():
        pub = vdata.get("public", {}) or {}
        name = vdata.get("name") or pub.get("name") or vid
        for c in incomings_by_target.get(str(vid), []):
            if (c.get("eta") or 0) <= 0:
                continue
            incomings.append({
                "village_id": str(vid),
                "village_name": name,
                "command_id": c.get("command_id"),
                "origin_name": c.get("origin_name"),
                "origin_coords": c.get("origin_coords"),
                "player_name": c.get("player_name"),
                "arrival": c.get("arrival"),
                "arrival_ms": c.get("arrival_ms"),
                "eta": c.get("eta"),
                "tag": c.get("tag") or c.get("game_label") or c.get("tag_auto"),
            })
    incomings.sort(key=lambda c: c.get("arrival") or 0)
    return incomings


class CSnipeOverview:
    """View-model for the Defense page's C-snipe tab: the incomings that can be
    sniped, each village's troops at home (to prefill the send form) and a
    suggested barbarian target far enough out that the outgoing command is
    still under way at the cancel moment.
    """

    # Prefilled with the full count at home (the defensive stack that should
    # dodge and return); the rest of FORM_UNITS start at 0. Militia never
    # travels and nobles are never risked on a snipe, so neither is offered.
    PREFILL_UNITS = ["spear", "sword", "archer", "marcher", "heavy", "spy"]
    FORM_UNITS = ["spear", "sword", "archer", "marcher", "heavy", "spy",
                  "axe", "light", "knight", "ram", "catapult"]

    DEFAULT_LEAD_MIN = 18
    # Default "land +ms after the first hit". The engine biases the cancel so
    # the return is never early but may run up to ~2x ping late; anything much
    # under ~150ms is fake precision on a residential connection. For a lone
    # incoming (no train behind it) 300-500ms is the safe choice.
    DEFAULT_AIM_MS = 150

    @staticmethod
    def _suggest_barb(loc, barbs, speeds, world_speed, unit_speed, lead_seconds):
        """Nearest barb whose travel time keeps the troops under way at the
        cancel moment even for a pure-heavy send; slower units only add margin."""
        if not (field_distance and unit_travel_seconds) or not loc:
            return None
        base = speeds.get("heavy") or 11
        required = lead_seconds / 2.0 + 180
        best = None
        for barb in barbs:
            distance = field_distance(loc, barb["location"])
            travel = unit_travel_seconds(distance, base, world_speed, unit_speed)
            if travel < required:
                continue
            if best is None or distance < best["distance"]:
                best = {
                    "x": int(barb["location"][0]), "y": int(barb["location"][1]),
                    "name": barb.get("name"),
                    "distance": round(distance, 1),
                    "travel_min_heavy": int(travel // 60),
                }
        return best

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}
        now = int(time.time())

        world_cfg = (DataReader.cache_grab("world") or {}).get("config") or {}
        cancel_seconds = int(world_cfg.get("command_cancel_time") or 600)
        ws, us, speeds = DataReader.world_speeds()
        barbs = [
            v for v in village_db.values()
            if str(v.get("owner")) == "0"
            and isinstance(v.get("location"), list) and len(v["location"]) == 2
        ]

        villages = {}
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            loc = pub.get("location")
            avail = vdata.get("available_troops", {}) or {}
            name = vdata.get("name") or pub.get("name") or vid
            villages[str(vid)] = {
                "id": str(vid),
                "name": name,
                "coords": loc,
                "home": {u: OverviewBuilder._to_int(avail.get(u))
                         for u in cls.FORM_UNITS},
                "barb": cls._suggest_barb(loc, barbs, speeds, ws, us,
                                          cls.DEFAULT_LEAD_MIN * 60),
            }
        incomings = live_incomings(managed, village_db)

        # Active snipes first (soonest start first), then newest history.
        snipes = DataReader.csnipe_grab()
        active = [s for s in snipes if s.get("status") in ("armed", "running")]
        done = [s for s in snipes if s.get("status") not in ("armed", "running")]
        active.sort(key=lambda s: s.get("start_ts") or 0)
        done.sort(key=lambda s: s.get("finished") or s.get("created") or 0,
                  reverse=True)

        return {
            "incomings": incomings,
            "villages": villages,
            "snipes": active + done,
            "active_count": len(active),
            "test_active_count": sum(1 for s in active if s.get("test")),
            "form_units": cls.FORM_UNITS,
            "prefill_units": cls.PREFILL_UNITS,
            "cancel_seconds": cancel_seconds,
            "default_lead_min": cls.DEFAULT_LEAD_MIN,
            "default_aim_ms": cls.DEFAULT_AIM_MS,
            "now": now,
        }


class SnipeOverview:
    """View-model for the Defense page's Snipe tab: incoming attacks, every
    managed village's defensive troops at home + coordinates, the cached
    in-game village groups and the world speeds - the tab computes the
    per-village/per-pace snipe options client-side from these, Toxic-Donut
    style, and arms the checked ones in one go.
    """

    # Units offered as snipe support, and the paces the options are built
    # from. Spy/militia are useless as support and nobles are never risked.
    SNIPE_UNITS = ["spear", "sword", "archer", "marcher", "heavy", "knight"]

    DEFAULT_OFFSET_MS = snipe_engine.DEFAULT_OFFSET_MS if snipe_engine else -100
    DEFAULT_MIN_PCT = snipe_engine.DEFAULT_MIN_PCT if snipe_engine else 80

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}

        villages = {}
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            avail = vdata.get("available_troops", {}) or {}
            villages[str(vid)] = {
                "id": str(vid),
                "name": vdata.get("name") or pub.get("name") or vid,
                "coords": pub.get("location"),
                "home": {u: OverviewBuilder._to_int(avail.get(u))
                         for u in cls.SNIPE_UNITS},
            }

        ws, us, speeds = DataReader.world_speeds()
        snipes = DataReader.snipe_grab()
        active = [s for s in snipes if s.get("status") in ("armed", "running")]
        done = [s for s in snipes if s.get("status") not in ("armed", "running")]
        active.sort(key=lambda s: s.get("send_est_ts") or s.get("start_ts") or 0)
        done.sort(key=lambda s: s.get("finished") or s.get("created") or 0,
                  reverse=True)

        return {
            "incomings": live_incomings(managed, village_db),
            "villages": villages,
            "groups": DataReader.groups_grab(),
            "snipes": active + done,
            "active_count": len(active),
            "units": cls.SNIPE_UNITS,
            "speeds": {u: speeds.get(u) for u in cls.SNIPE_UNITS
                       if speeds.get(u)},
            "world_speed": ws,
            "unit_speed": us,
            "default_offset_ms": cls.DEFAULT_OFFSET_MS,
            "default_min_pct": cls.DEFAULT_MIN_PCT,
            "now": int(time.time()),
        }


class PlayerFarmOverview:
    """View-model for the Farms page's player-farm tab: the hit list with its
    report verdicts and totals, plus the managed villages (troops at home,
    coords) needed by the add form.
    """

    FORM_UNITS = ["spear", "sword", "axe", "spy", "light", "marcher", "heavy"]
    DEFAULT_UNITS = {"light": 35, "spy": 1}
    DEFAULT_INTERVAL_MIN = 60

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        now = int(time.time())

        villages = []
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            avail = vdata.get("available_troops", {}) or {}
            villages.append({
                "id": str(vid),
                "name": vdata.get("name") or pub.get("name") or vid,
                "coords": pub.get("location"),
                "home": {u: OverviewBuilder._to_int(avail.get(u))
                         for u in cls.FORM_UNITS},
            })
        villages.sort(key=lambda v: v["name"])

        world_speed, _us, _speeds = DataReader.world_speeds()
        carry = DataReader.light_carry()
        reports = DataReader.cache_grab("reports")

        farms = []
        for f in DataReader.playerfarm_grab():
            f = dict(f)
            interval_min = int(f.get("interval_min") or cls.DEFAULT_INTERVAL_MIN)
            last_sent = int(f.get("last_sent") or 0)
            if not f.get("next_due"):  # pre-jitter entries / never sent
                f["next_due"] = (last_sent + interval_min * 60) if last_sent else now
            f["due_now"] = f["status"] == "active" and int(f["next_due"]) <= now
            # Target economy from the newest scout intel: hourly production,
            # what's estimated to be inside right now, and the light cavalry
            # needed to carry one interval's worth of production.
            when, buildings, resources = DataReader.newest_scout_intel(
                f.get("target_id"), reports)
            if buildings and playerfarm:
                eco = playerfarm.estimate_economy(
                    buildings, resources, when, world_speed)
                eco["suggested_light"] = playerfarm.suggest_light(
                    eco["hourly_total"], interval_min, carry)
                f["economy"] = eco
            else:
                f["economy"] = None
            farms.append(f)
        # Active first, then paused, stopped last; alphabetical within a group.
        order = {"active": 0, "paused": 1, "stopped": 2}
        farms.sort(key=lambda f: (order.get(f.get("status"), 3),
                                  f.get("target_name") or ""))

        config = (data.get("config") or {}).get("farms") or {}
        return {
            "farms": farms,
            "villages": villages,
            "form_units": cls.FORM_UNITS,
            "default_units": cls.DEFAULT_UNITS,
            "default_interval_min": cls.DEFAULT_INTERVAL_MIN,
            "enabled": bool(config.get("player_farm", True)),
            "priority": bool(config.get("player_farm_priority", True)),
            "night_skip": bool(config.get("player_farm_night_skip", True)),
            "jitter": config.get("player_farm_jitter",
                                 getattr(playerfarm, "INTERVAL_JITTER", 0.10)),
            "profit_factor": getattr(playerfarm, "PROFIT_FACTOR", 2.0),
            "light_carry": carry,
            "now": now,
        }


class BotManager:
    def __init__(self):
        # world key ("" = default) -> pid we started, so each world's bot is
        # tracked and controlled independently.
        self._pids = {}

    @staticmethod
    def _world_key(world):
        """Normalise a world name to a safe key; "" means the default world."""
        if world and str(world).strip():
            return os.path.basename(str(world).strip())
        return ""

    @staticmethod
    def _cmdline_world(cmdline):
        """The --world value from a process command line, or None (default)."""
        parts = [str(p) for p in cmdline]
        for i, part in enumerate(parts):
            if part == "--world" and i + 1 < len(parts):
                return BotManager._world_key(parts[i + 1])
            if part.startswith("--world="):
                return BotManager._world_key(part.split("=", 1)[1])
        return ""

    @staticmethod
    def find_bot_pid(world=None):
        """Locate the running twb.py process for a given world.

        Matches a python process running twb.py whose --world matches (default
        world = no --world). Scanning by command line keeps the status accurate
        even when the bot was started by hand rather than through this UI.
        """
        want = BotManager._world_key(world)
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            runs_python = any("python" in str(part).lower() for part in cmdline)
            runs_script = any(str(part).strip("'\"").endswith("twb.py") for part in cmdline)
            if runs_python and runs_script and BotManager._cmdline_world(cmdline) == want:
                return proc.info["pid"]
        return None

    def is_running(self, world=None):
        # Trust a process we started ourselves, otherwise detect an externally
        # started bot so the status is accurate however the bot was launched.
        key = self._world_key(world)
        pid = self._pids.get(key)
        if pid and psutil.pid_exists(pid):
            return True
        detected = self.find_bot_pid(world)
        if detected:
            self._pids[key] = detected
            return True
        self._pids.pop(key, None)
        return False

    def start(self, world=None):
        key = self._world_key(world)
        if self.is_running(world):
            print("Bot already running (%s), skipping start" % (key or "default"))
            return
        wd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        log_name = ("bot_%s.log" % key) if key else "bot.log"
        log_path = os.path.join(wd, "worlds", key, log_name) if key else os.path.join(wd, log_name)
        cmd = [sys.executable, "twb.py"] + (["--world", key] if key else [])
        with open(log_path, "a") as log_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=wd,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,   # detach from web server's process group
            )
        self._pids[key] = proc.pid
        print("Bot started (pid=%d, log=%s)" % (proc.pid, log_path))

    def stop(self, world=None):
        key = self._world_key(world)
        target = self._pids.get(key)
        if not (target and psutil.pid_exists(target)):
            target = self.find_bot_pid(world)
        if target:
            os.kill(target, signal.SIGTERM)
            self._pids.pop(key, None)
            print("Bot stopped successfully (%s)" % (key or "default"))
