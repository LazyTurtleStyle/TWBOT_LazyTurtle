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

from core.instance_lock import InstanceLock
from game import attack_scheduler, attack_plan

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
    from game import noblebarb
except Exception:  # pragma: no cover - dashboard still works without it
    noblebarb = None

try:
    from game import accountmanager
except Exception:  # pragma: no cover - dashboard still works without it
    accountmanager = None

try:
    from game import events as game_events
except Exception:  # pragma: no cover - dashboard still works without it
    game_events = None

try:
    from game.livetroops import read_home_troops
except Exception:  # pragma: no cover - dashboard still works without live reads
    read_home_troops = None

try:
    from game import villagenotes
except Exception:  # pragma: no cover - dashboard still works without notes
    villagenotes = None

try:
    from game import worldvillages
except Exception:  # pragma: no cover - dashboard still works without the map file
    worldvillages = None

try:
    from game import reportanalysis
except Exception:  # pragma: no cover - dashboard still works without it
    reportanalysis = None

try:
    from game import flags as flagmodule
except Exception:  # pragma: no cover - dashboard still works without it
    flagmodule = None

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
        "name": name,
        "label": "%s → level %s" % (name, level) if level else name,
    }


def _short_duration(seconds):
    """'3h 20m' / '45s' - for saying how far off something is, not for clocks."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh %dm" % (seconds // 3600, seconds % 3600 // 60)


def parse_hq_queue(entries):
    """Display data for the jobs sitting in the in-game HQ queue.

    Takes what the bot cached from the game's own build queue (building, level,
    when it finishes) - not the bot's planned build order, which is a different
    and much longer list.
    """
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item = parse_queue_entry("%s:%s" % (
            entry.get("building") or "", entry.get("level") or ""))
        item["ready_at"] = entry.get("ready_at")
        item["duration"] = entry.get("duration")
        # Seconds left, so the cell reads right before the page's ticker takes
        # over on the first second.
        ready = item["ready_at"]
        item["eta"] = max(0, int(ready) - int(time.time())) if ready else None
        out.append(item)
    return out


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
    def ensure_data_dir(*parts):
        """data_path, but the directory it points at is created first.

        cache/ is gitignored, so a freshly unzipped copy has no cache directory
        until the bot has run once (twb.py creates them at startup). Anything the
        dashboard writes before that - pasting the very first cookie, most
        obviously - has to create it, or the save fails with a FileNotFoundError
        and the page just reports "Error saving session".
        """
        path = DataReader.data_path(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def server_clock_offset():
        """Seconds to add to host time to read the game server's wall clock.

        Written by the bot each cycle (core.server_clock). Read here through
        data_path rather than FileManager because the web process serves every
        world and has no single active data root. 0.0 when unknown, which is the
        old behaviour of assuming host and server agree.
        """
        try:
            path = DataReader.data_path("cache", "server_clock.json")
            if os.path.exists(path):
                with open(path) as f:
                    value = json.load(f).get("offset_seconds")
                if isinstance(value, (int, float)):
                    return float(value)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def troop_locations():
        """Where the account's units stand, as the bot last read it from the
        game (cache/troops_moving.json, written by update_troop_movements).

        {"home": {}, "support": {}, "moving": {}, "by_village": {vid: {"own",
        "in_village", "elsewhere", "moving"}}, "when": ts} - or {} when the bot
        has not cached it yet. This is the only source that knows about troops
        stationed in another village: a village's own snapshot never lists them.
        """
        try:
            path = DataReader.data_path("cache", "troops_moving.json")
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def home_troop_reading():
        """Units standing in each village, from the account-wide reading.

        Returns ({village_id: {unit: count}}, age_seconds) - the "own" figures
        of cache/troops_moving.json, which the bot refreshes every few minutes
        for the whole account in one pass. A village missing from it has no
        reading at all (rather than an empty garrison), so callers can fall
        back to that village's own snapshot and say which one they are showing.
        """
        locations = DataReader.troop_locations()
        if not isinstance(locations, dict):
            return {}, None
        by_village = locations.get("by_village") or {}
        # A partial write carries the previous breakdown forward, so age the
        # figures by when that breakdown was actually read, not by the file.
        when = (OverviewBuilder._to_int(locations.get("complete_when"))
                or OverviewBuilder._to_int(locations.get("when")))
        age = (int(time.time()) - when) if when else None
        reading = {}
        for vid, entry in by_village.items():
            own = (entry or {}).get("own")
            if isinstance(own, dict):
                reading[str(vid)] = own
        return reading, age

    @staticmethod
    def troop_templates():
        """The player's rally-point troop templates as the bot last read them
        (cache/troop_templates.json): {id: {"name", "units", "use_all"}}.

        "units" already speaks the scheduler's language, so a template can fill
        the unit fields directly - a send-everything template comes through as
        "all" per unit and resolves at send time like any other 'all'.
        """
        try:
            path = DataReader.data_path("cache", "troop_templates.json")
            if os.path.exists(path):
                with open(path) as f:
                    return (json.load(f) or {}).get("templates") or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def troop_templates_expire():
        """Mark the cached templates stale so the bot re-reads them from the
        rally point on its next pass - for right after editing a template
        in-game. The old contents stay readable until then, so the picker never
        goes empty while waiting."""
        try:
            path = DataReader.data_path("cache", "troop_templates.json")
            if not os.path.exists(path):
                return False
            with open(path) as f:
                cached = json.load(f) or {}
            cached["when"] = 0
            with open(path, "w") as f:
                json.dump(cached, f, indent=2)
            return True
        except Exception:
            return False

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
        "since": unix ts or None, "heartbeat_age": seconds or None,
        "started": unix ts of the running process or None}.
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
                    "heartbeat_age": None, "started": None}

        heartbeat = DataReader.data_path("cache", "heartbeat.json")
        blank = {"stalled": False, "reason": None, "since": None,
                 "heartbeat_age": None, "started": None}
        if not os.path.exists(heartbeat):
            return blank
        try:
            with open(heartbeat) as f:
                beat = json.load(f) or {}
            ts = int(beat.get("ts") or 0)
            # Absent on a bot predating the field; callers fall back to wall time.
            started = int(beat.get("started") or 0) or None
        except Exception:
            return blank

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
                "since": ts, "heartbeat_age": age, "started": started}

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

    # Memo for cache_grab, invalidated by content rather than by a timer.
    # sync() is the shared data loader behind nearly every dashboard page, so
    # each page view re-parsed the whole cache directory - and cache/reports is
    # tens of thousands of files (measured 2026-08-03: ~44s and 29.5k file reads
    # for one pass on a loaded box). Fingerprinting the directory instead costs
    # ~0.1s, so the parse only reruns when a file is actually added or rewritten.
    # A time-based memo was the obvious alternative but is wrong here: a cold
    # parse can outlast any sane TTL, so it could expire midway through the very
    # render it was meant to speed up. Keyed on the resolved path, so switching
    # worlds never serves another world's cache.
    _grab_memo = {}
    _grab_memo_lock = threading.Lock()

    @staticmethod
    def _dir_signature(path):
        """Cheap fingerprint of a cache dir: (file_count, newest_mtime).

        Statting is far cheaper than parsing, and this catches both new files
        (count) and rewritten ones (mtime), which is every way the bot changes
        a cache directory.
        """
        count = 0
        newest = 0.0
        try:
            with os.scandir(path) as it:
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
            return (0, 0.0)
        return (count, newest)

    @staticmethod
    def cache_grab(cache_location):
        output = {}
        c_path = DataReader.data_path("cache", cache_location)
        if not os.path.isdir(c_path):
            return output

        signature = DataReader._dir_signature(c_path)
        with DataReader._grab_memo_lock:
            hit = DataReader._grab_memo.get(c_path)
            if hit and hit[0] == signature:
                return hit[1]

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

        with DataReader._grab_memo_lock:
            # Deliberately the signature taken *before* parsing. If the bot wrote
            # a report while we were reading, the post-parse signature would
            # match a result that never contained it and pin that stale copy
            # until the next write; the pre-parse one simply misses next time and
            # re-reads. Same for the broken entries removed above - one extra
            # pass and the memo settles.
            DataReader._grab_memo[c_path] = (signature, output)
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
        during forced peace. The windows are wall-clock strings in the *server's*
        timezone, so the arrival moment is read on the server's clock too - on a
        host in another timezone, reading it locally shifts every window."""
        config = DataReader.config_grab()
        windows = ((config.get("farms") or {}).get("forced_peace_times")) or []
        arrival = (datetime.datetime.fromtimestamp(arrival_ts)
                   + datetime.timedelta(seconds=DataReader.server_clock_offset()))
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
    def schedule_create(origin_id, target_x, target_y, units, arrival_ts,
                        train=None, dry_run=False, support=False):
        """Queue a timed attack scheduled to LAND at arrival_ts (unix seconds).
        The send moment is back-calculated from the slowest selected unit's
        travel time. Returns (entry, error_message).

        With `train` ({"mode": "front"|"even", "escort": {unit: n}}) the stack is
        split into one command per noble, all fired back-to-back at the send
        moment. dry_run=True validates and returns the entry without queueing it,
        which is what the dashboard's wave preview uses. support=True sends the
        troops as support instead of an attack (imported plans mark those rows,
        and sending one as an attack would hit an ally)."""
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
            # Milliseconds are kept. The game computes travel as a whole number
            # of seconds, so a command's arrival carries the milliseconds of its
            # send - a plan that deliberately spaces commands sub-second only
            # survives being queued if that survives here. A whole second is
            # stored as an int, so a command written without milliseconds looks
            # exactly as it always did.
            arrival_ts = round(float(arrival_ts), 3)
            if arrival_ts == int(arrival_ts):
                arrival_ts = int(arrival_ts)
        except (TypeError, ValueError):
            return None, "invalid arrival time"

        selected = {}
        for unit, count in (units or {}).items():
            if isinstance(count, str) and count.strip().lower() == "all":
                # Resolved by the bot at send time to whatever is home then,
                # so the command still fires when the village holds fewer
                # troops than it does now.
                selected[unit] = "all"
                continue
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                selected[unit] = count
        if not selected:
            return None, "no units selected"

        waves = None
        dynamic = False
        notes = []
        if train and support:
            return None, "a support command cannot be a noble train"
        if train and str(train.get("mode")) == "copies":
            # A fake train: the same small stack sent several times from one
            # village, landing back-to-back the way a noble train does. The
            # runner already fires a list of waves without caring what is in
            # them - only the splitting was ever about nobles - so this just
            # hands it the same units N times.
            count = max(2, min(int(train.get("waves") or 2), 10))
            if attack_scheduler.has_all(selected):
                return None, ("a fake train needs exact counts, not 'all' - "
                              "every wave sends the same stack")
            waves = [dict(selected) for _ in range(count)]
            notes.append("%d identical waves, %s each"
                         % (count, ", ".join("%s %s" % (u, n)
                                             for u, n in selected.items())))
        elif train:
            home = {}
            for unit, count in (origin.get("available_troops") or {}).items():
                try:
                    home[unit] = int(count)
                except (TypeError, ValueError):
                    continue
            # "all" is only a real count at send time, so the split shown here is
            # a preview off the last snapshot; the bot re-reads the rally point
            # and re-splits just before launching (resolve_train_waves).
            dynamic = attack_scheduler.has_all(selected)
            if dynamic and not home:
                return None, ("no troop snapshot for this village yet - "
                              "type the counts instead of 'all'")
            counts = attack_scheduler.resolve_all_units(selected, home)
            # A front-loaded train takes the escort out of the stack it is
            # sending, so "25 heavy each" needs 25 x every following noble. Ask
            # for more than the stack carries and there are two different
            # situations, worth treating differently:
            #
            #   the stack has some, just not that many - a shortage. Trim to
            #     what fits, which is exactly what the send-time re-split would
            #     do anyway (fit_escort), and say so.
            #   the stack has none at all, or not even one per wave - a mistake,
            #     usually an escort unit this village does not train. Left for
            #     split_train to refuse, because silently sending bare nobles is
            #     not what anyone meant.
            escort = dict(train.get("escort") or {})
            followers = max(counts.get("snob", 0) - 1, 0)
            # Only front-loaded uses an escort at all: an even split divides the
            # whole stack by the number of nobles and never reads it. Trimming
            # (and saying so) in even mode would describe something that is not
            # going to happen.
            if followers and (train.get("mode") or "front") == "front":
                for unit, per_wave in sorted(escort.items()):
                    have = counts.get(unit, 0)
                    if have and 0 < have // followers < per_wave:
                        escort[unit] = have // followers
                        notes.append(
                            "escort trimmed to %d %s per wave - the stack "
                            "carries %d, and you asked for %d each"
                            % (escort[unit], unit, have, per_wave))
            waves, err = attack_scheduler.split_train(
                counts, mode=(train.get("mode") or "front"), escort=escort)
            if err:
                return None, err
            # Fewer nobles at home than the train asks for is worth saying out
            # loud, but not worth refusing: the command may be hours away, a
            # noble may well finish before then, and the send-time split shrinks
            # to whatever is actually standing there rather than failing.
            at_home = home.get("snob", 0)
            if at_home < len(waves):
                notes.append(
                    "only %d noble%s at home right now - unless more finish "
                    "before it leaves, this goes as %d wave%s"
                    % (at_home, "" if at_home == 1 else "s",
                       max(at_home, 1), "" if at_home == 1 else "s"))

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
            # Whole seconds: this only decides when the command is claimed and
            # pre-staged. The launch moment is worked out from the server's own
            # travel time when it fires, and that is what carries the ms.
            "send_ts": int(arrival_ts - travel),
            "travel_seconds": int(travel),
            "distance": round(distance, 1),
            "status": "pending",
            "created": int(time.time()),
        }
        if support:
            entry["support"] = True
        if notes:
            # Things worth knowing about a command that was still queued.
            entry["notes"] = notes
        if waves:
            # The waves are what actually gets sent; `units` stays the whole
            # stack so every existing view keeps reading the same field.
            entry["waves"] = waves
            entry["train"] = {
                "mode": train.get("mode") or "front",
                # A fake train is never re-split at send time: there are no
                # nobles to count, and each wave is meant to be identical.
                "fake": str(train.get("mode")) == "copies",
                "nobles": len(waves),
                "escort": train.get("escort") or {},
                # Re-read the village and re-split at send time. Leave room for
                # a couple of nobles that finish training between now and then;
                # the pre-stage window is sized for max_waves, so the train can
                # grow that far and no further.
                "dynamic": dynamic,
                "max_waves": len(waves) + (2 if dynamic else 0),
            }
        if dry_run:
            return entry, None
        # Append through the shared, locked, atomic store so the bot's concurrent
        # status writes can't clobber this command (and vice versa).
        attack_scheduler.add_command(entry, path=DataReader.schedule_path())
        return entry, None

    @staticmethod
    def schedule_display():
        """The queue as the dashboard should show it.

        A train queued with "all" stores the split that its troop snapshot
        implied at the time, which goes stale the moment a noble finishes or a
        farm run comes home. Recompute those previews against the newest
        snapshot - the same arithmetic the bot runs against the live rally point
        when the command is due - so the table shows what would be sent now
        instead of what was guessed then.
        """
        commands = DataReader.schedule_grab()
        managed = DataReader.cache_grab("managed")
        for command in commands:
            spec = command.get("train") or {}
            if command.get("status") != "pending" or not spec.get("dynamic"):
                continue
            origin = managed.get(str(command.get("origin_id"))) or {}
            home = {}
            for unit, count in (origin.get("available_troops") or {}).items():
                try:
                    home[unit] = int(count)
                except (TypeError, ValueError):
                    continue
            if not home:
                continue
            counts = attack_scheduler.resolve_all_units(command.get("units") or {}, home)
            budget = int(spec.get("max_waves") or 0) or counts.get("snob", 0)
            nobles = min(counts.get("snob", 0), budget)
            if nobles < 2:
                continue  # nothing to preview as a train right now
            counts["snob"] = nobles
            escort, _notes = attack_scheduler.fit_escort(
                spec.get("escort"), counts, nobles - 1)
            waves, err = attack_scheduler.split_train(
                counts, mode=spec.get("mode") or "front", escort=escort)
            if waves and not err:
                command["waves"] = waves
        return commands

    @staticmethod
    def schedule_retime(command_id, arrival_ts):
        """Move a queued command's arrival, re-deriving when it has to leave.

        A planner solves for "these commands land together", which is rarely
        what you want once they are queued - the nuke has to land before the
        train, a fake has to arrive a moment earlier than the real one. Rather
        than making the import guess at that, the queue itself is editable.

        The travel time is not recalculated: it belongs to the units this
        command is already carrying, and those have not changed. So the send is
        simply the new arrival minus that, and it has to stay far enough ahead
        for the scheduler to still claim and pre-stage the command - which for a
        train is several waves' worth of round trips, hence command_prestage
        rather than a flat margin.

        Returns (entry, error). Runs inside the queue's own lock, so a command
        the bot is claiming in the same moment cannot be moved out from under it.
        """
        try:
            arrival_ts = round(float(arrival_ts), 3)
            if arrival_ts == int(arrival_ts):
                arrival_ts = int(arrival_ts)
        except (TypeError, ValueError):
            return None, "invalid arrival time"
        if DataReader._forced_peace_conflict(arrival_ts):
            return None, "arrival falls inside a forced-peace window"

        outcome = {}

        def mutate(commands):
            for command in commands:
                if command.get("id") != command_id:
                    continue
                if command.get("status") != "pending":
                    outcome["error"] = ("this command is %s, so it can no longer "
                                        "be moved" % command.get("status"))
                    return
                travel = int(command.get("travel_seconds") or 0)
                if travel <= 0:
                    outcome["error"] = "no travel time on file for this command"
                    return
                send_ts = int(arrival_ts - travel)
                lead = attack_scheduler.command_prestage(command)
                now = time.time()
                if send_ts <= now:
                    outcome["error"] = ("too late - it would have had to leave "
                                        "%s ago" % _short_duration(now - send_ts))
                    return
                if send_ts <= now + lead:
                    outcome["error"] = (
                        "too close - it would leave in %s, and this command needs "
                        "%ds of lead time to be claimed and pre-staged"
                        % (_short_duration(send_ts - now), lead))
                    return
                command["arrival_ts"] = arrival_ts
                command["send_ts"] = send_ts
                command["retimed"] = int(time.time())
                outcome["entry"] = command
                return
            outcome["error"] = "no such command in the queue"

        attack_scheduler.update(mutate, path=DataReader.schedule_path())
        return outcome.get("entry"), outcome.get("error")

    @staticmethod
    def schedule_retroop(command_id, units, waves=None):
        """Change what a queued command carries, keeping the moment it lands.

        Retiming can leave the travel time alone - the units did not change.
        Swapping the stack cannot: a fake train moved off light cavalry onto
        axemen halves its speed, so the same arrival now needs it to leave hours
        earlier. The arrival is what the player set and what the rest of the
        plan is timed around, so that is what is held fixed; the send moment is
        re-derived, and the edit is refused when the new stack can no longer get
        there in time rather than silently landing late.

        `waves` re-counts a fake train (identical copies). A noble train is
        re-split from the new stack instead, because there the wave count is the
        number of nobles in it.

        Returns (entry, error).
        """
        outcome = {}

        def mutate(commands):
            for command in commands:
                if command.get("id") != command_id:
                    continue
                if command.get("status") != "pending":
                    outcome["error"] = ("this command is %s, so what it carries "
                                        "can no longer be changed"
                                        % command.get("status"))
                    return
                entry, error = DataReader._retroop_one(command, units, waves)
                outcome["error"] = error
                outcome["entry"] = entry
                return
            outcome["error"] = "no such command in the queue"

        attack_scheduler.update(mutate, path=DataReader.schedule_path())
        return outcome.get("entry"), outcome.get("error")

    @staticmethod
    def _retroop_one(command, units, waves):
        """The body of schedule_retroop, run under the queue lock. Mutates
        `command` in place and returns (entry, error) - on an error nothing has
        been written, so the caller can leave the queue as it was."""
        selected = {}
        for unit, count in (units or {}).items():
            if isinstance(count, str) and count.strip().lower() == "all":
                selected[unit] = "all"
                continue
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                selected[unit] = count
        if not selected:
            return None, "no units selected"

        spec = dict(command.get("train") or {})
        is_copies = bool(spec) and str(spec.get("mode")) == "copies"
        notes = []
        new_waves = None

        if is_copies:
            if attack_scheduler.has_all(selected):
                return None, ("a fake train needs exact counts, not 'all' - "
                              "every wave sends the same stack")
            count = len(command.get("waves") or []) or 2
            if waves is not None:
                try:
                    count = int(waves)
                except (TypeError, ValueError):
                    return None, "invalid wave count"
            count = max(2, min(count, 10))
            new_waves = [dict(selected) for _ in range(count)]
            notes.append("%d identical waves, %s each"
                         % (count, ", ".join("%s %s" % (u, n)
                                             for u, n in selected.items())))
            spec["nobles"] = count
            spec["max_waves"] = count
        elif command.get("waves"):
            # A noble train: the wave count is however many nobles the new stack
            # holds, so it is re-split rather than re-counted.
            managed = DataReader.cache_grab("managed")
            origin = managed.get(str(command.get("origin_id"))) or {}
            home = {}
            for unit, count in (origin.get("available_troops") or {}).items():
                try:
                    home[unit] = int(count)
                except (TypeError, ValueError):
                    continue
            counts = attack_scheduler.resolve_all_units(selected, home)
            escort, escort_notes = attack_scheduler.fit_escort(
                spec.get("escort"), counts, max(counts.get("snob", 0) - 1, 0))
            notes.extend(escort_notes or [])
            new_waves, err = attack_scheduler.split_train(
                counts, mode=spec.get("mode") or "front", escort=escort)
            if err:
                return None, err
            spec["escort"] = escort
            spec["nobles"] = len(new_waves)
            spec["dynamic"] = attack_scheduler.has_all(selected)
            spec["max_waves"] = len(new_waves)

        if not (field_distance and unit_travel_seconds):
            return None, "travel-time helpers unavailable"
        managed = DataReader.cache_grab("managed")
        origin = managed.get(str(command.get("origin_id"))) or {}
        loc = (origin.get("public") or {}).get("location")
        if not loc or len(loc) != 2:
            return None, "unknown origin village"
        ws, us, speeds = DataReader.world_speeds()
        distance = field_distance((loc[0], loc[1]),
                                  (int(command["target_x"]), int(command["target_y"])))
        travels = [unit_travel_seconds(distance, speeds[u], ws, us)
                   for u in selected if speeds.get(u)]
        if not travels:
            return None, "no travel speed for the selected units"
        travel = max(travels)  # the slowest unit dictates arrival

        arrival_ts = command.get("arrival_ts")
        # Subtract before rounding, the way the command was created: rounding
        # the travel first moves the send by up to a second, which on a queue
        # timed to the millisecond is a change nobody asked for.
        send_ts = int(arrival_ts - travel)
        lead = attack_scheduler.command_prestage(command)
        now = time.time()
        if send_ts <= now:
            return None, ("too slow - %s would have had to leave %s ago to land "
                          "at the same moment"
                          % (DataReader._stack_label(selected),
                             _short_duration(now - send_ts)))
        if send_ts <= now + lead:
            return None, ("too slow - %s would have to leave in %s, and this "
                          "command needs %ds of lead time to be claimed and "
                          "pre-staged"
                          % (DataReader._stack_label(selected),
                             _short_duration(send_ts - now), lead))

        # Under the world's minimum attack size the rally point refuses the
        # command at the moment it should be launching. Worth saying, not worth
        # refusing: the village may well have grown or shrunk by then, and the
        # player asked to be warned rather than blocked.
        floor = float((DataReader.cache_grab("world") or {})
                      .get("config", {}).get("fake_limit") or 0)
        points = 0
        try:
            points = int((origin.get("public") or {}).get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        if floor and points and not attack_scheduler.has_all(selected):
            need = int(points * floor / 100.0)
            pop = sum(UNIT_POP.get(u, 1) * n for u, n in selected.items()
                      if isinstance(n, int))
            if pop < need:
                notes.append(
                    "under this world's minimum attack size: %d population, and "
                    "%s needs %d - the rally point will refuse it"
                    % (pop, origin.get("name") or command.get("origin_name"), need))

        command["units"] = selected
        command["travel_seconds"] = int(travel)
        command["send_ts"] = send_ts
        command["distance"] = round(distance, 1)
        command["retrooped"] = int(time.time())
        if new_waves is not None:
            command["waves"] = new_waves
            command["train"] = spec
        command["notes"] = notes
        return command, None

    @staticmethod
    def _stack_label(units):
        """"axe 150, ram 1" - what a refusal is talking about, so the message
        names the stack the player just picked rather than "the new units"."""
        return ", ".join("%s %s" % (u, n) for u, n in units.items()) or "that stack"

    @staticmethod
    def schedule_cancel(command_id):
        return attack_scheduler.cancel_command(command_id, path=DataReader.schedule_path())

    @staticmethod
    def schedule_cancel_many(command_ids):
        """Cancel a set of queued commands at once. Returns how many were still
        pending and therefore actually cancelled."""
        return attack_scheduler.cancel_commands(command_ids,
                                                path=DataReader.schedule_path())

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
    def _csnipe_target_kind(tx, ty):
        """What kind of command can be sent at (tx|ty): ("attack"|"support"|
        None, target_name).

        The game decides this, not the user: a barbarian village cannot be
        supported and one of your own cannot be attacked. None means the map
        cache does not know the village, in which case whatever was asked for
        is let through - refusing on missing cache data would block a target
        the player can see perfectly well in game.
        """
        managed = set(str(v) for v in (DataReader.cache_grab("managed") or {}))
        for vid, v in (DataReader.cache_grab("villages") or {}).items():
            loc = v.get("location")
            if not loc or len(loc) != 2:
                continue
            try:
                if int(loc[0]) != tx or int(loc[1]) != ty:
                    continue
            except (TypeError, ValueError):
                continue
            name = v.get("name")
            name = name if isinstance(name, str) and name else None
            if str(v.get("owner")) == "0":
                return "attack", name
            if str(vid) in managed:
                return "support", name
            return None, name          # somebody else's - either is possible
        return None, None

    @staticmethod
    def csnipe_arm(village_id, incoming_id, first_hit_ms, aim_ms, lead_min,
                   target_x, target_y, units, test=False, window_ms=None,
                   support=False):
        """Arm a cancel snipe: the bot sends `units` from the attacked village
        toward (target_x|target_y) and cancels at the halfway moment so they
        land back home at first_hit_ms + aim_ms (epoch milliseconds).
        With support=True the outgoing command is support rather than an
        attack - the dodge is identical, but a cancel that never goes through
        parks the troops in the target village instead of throwing them at a
        barbarian that may well defend itself.
        With window_ms > 0 (alpha) the return must land within that many ms
        PAST the target: the engine aims mid-window and re-fires missed sends
        until one lands inside it. 0/None keeps the late-safe one-shot mode.
        With test=True the entry is a dry run against a user-chosen return
        moment instead of an incoming attack - same engine path, just badged.
        Returns (entry, error_message)."""
        if csnipe is None:
            return None, "c-snipe engine unavailable"
        village_id = str(village_id)
        # Including the villages the bot has not run yet: a village taken this
        # afternoon is exactly the one being sniped for, and looking it up in
        # the raw snapshot cache refused to arm with "unknown village" while
        # the page listed its four incoming attacks quite happily.
        managed = OverviewBuilder._with_attacked_targets(
            DataReader.cache_grab("managed") or {},
            DataReader.cache_grab("villages") or {},
            OverviewBuilder._build_incomings(
                DataReader.cache_grab("villages") or {}))
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

        # The game will not let you support a barbarian or attack your own
        # village, and it refuses the whole command rather than adapting. Better
        # to say so here than to have the send fail at the millisecond it was
        # supposed to fire, with no second chance at that gap.
        support = bool(support)
        kind, target_name = DataReader._csnipe_target_kind(tx, ty)
        if kind == "attack" and support:
            return None, ("%d|%d is a barbarian village - it can be attacked, "
                          "not supported" % (tx, ty))
        if kind == "support" and not support:
            return None, ("%d|%d is your own village - send support to it, "
                          "you cannot attack it" % (tx, ty))

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
            "support": support,
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

    # -- in-game Account Manager ------------------------------------------
    # The plan is a list of (group, template) rows per screen, kept out of
    # config.json on purpose: the all-settings editor renders every key of a
    # section as one control, and would flatten a list of rows into a string
    # the moment anything on that page was saved.
    AM_SECTIONS = ("building", "troops", "research")

    @staticmethod
    def am_plans_path():
        """Resolved here, not in the game module: only this process knows which
        world the browser is looking at."""
        return DataReader.data_path("cache", "am_plans.json")

    @staticmethod
    def am_plans_grab():
        """The saved Account Manager plan, always with all three sections
        present: {"building": [...], "troops": [...], "research": [...],
        "run_now": bool, "refresh": bool}."""
        if accountmanager is None:
            return {s: [] for s in DataReader.AM_SECTIONS}
        return accountmanager.load_plans(path=DataReader.am_plans_path())

    @staticmethod
    def am_plans_save(section, rows):
        """Replace one section's rows. Ids are kept as the game writes them
        (digit strings); the names ride along so the page can still label a row
        whose template was renamed or deleted in game.

        Row order is the plan's meaning - the bot applies them top to bottom -
        so the list is stored exactly as the page sent it.
        """
        if section not in DataReader.AM_SECTIONS or accountmanager is None:
            return False
        clean = []
        for row in rows or []:
            group_id = str((row or {}).get("group_id") or "").strip()
            template_id = str((row or {}).get("template_id") or "").strip()
            if not group_id.isdigit() or not template_id.isdigit():
                continue
            clean.append({
                "group_id": group_id,
                "template_id": template_id,
                "group_name": str(row.get("group_name") or ""),
                "template_name": str(row.get("template_name") or ""),
            })
        DataReader.ensure_data_dir("cache")
        accountmanager.update_plans(lambda plans: plans.update({section: clean}),
                                    path=DataReader.am_plans_path())
        return True

    @staticmethod
    def am_request(kind):
        """Ask the bot to apply the plan ("run_now") or to re-read the
        manager's templates and per-village state ("refresh") on its next
        cycle. The bot owns every game request: it is the process with the
        session, the pacing and the captcha handling."""
        if kind not in ("run_now", "refresh") or accountmanager is None:
            return False
        DataReader.ensure_data_dir("cache")
        accountmanager.update_plans(lambda plans: plans.update({kind: True}),
                                    path=DataReader.am_plans_path())
        return True

    # -- rotating in-game events -------------------------------------------

    @staticmethod
    def minting_run_now():
        """Ask the bot for a resource run on its next cycle."""
        path = DataReader.data_path("cache", "minting.json")
        try:
            state = {}
            if os.path.exists(path):
                with open(path) as handle:
                    state = json.load(handle) or {}
            state["run_now"] = True
            DataReader.ensure_data_dir("cache")
            with open(path, "w") as handle:
                json.dump(state, handle, indent=2)
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def events_dir():
        return DataReader.data_path("cache", "events")

    @staticmethod
    def events_grab():
        """Every event the bot has seen, newest first. Read straight off the
        per-event files (world-aware, so the game module's own reader cannot be
        used here)."""
        out = []
        directory = DataReader.events_dir()
        if not os.path.isdir(directory):
            return out
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name)) as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if data:
                out.append(data)
        return sorted(out, key=lambda s: s.get("last_seen") or 0, reverse=True)

    @staticmethod
    def event_refresh(screen):
        """Ask the bot to re-read the running event on its next cycle. Needed
        because with auto-play off nothing would otherwise open the event page,
        and a dashboard that shows a day-old energy bar is worse than useless."""
        safe = "".join(c for c in str(screen) if c.isalpha() or c == "_")
        path = os.path.join(DataReader.events_dir(), "%s.json" % safe)
        if not os.path.exists(path):
            return False
        try:
            with open(path) as handle:
                state = json.load(handle) or {}
            state["refresh"] = True
            with open(path, "w") as handle:
                json.dump(state, handle, indent=2)
            return True
        except (OSError, ValueError):
            return False

    # -- village flags ------------------------------------------------------
    # Same shape as the Account Manager plan, and kept out of config.json for
    # the same reason: the all-settings editor renders every key of a section
    # as one control and would flatten a list of rows into a string.

    @staticmethod
    def flag_plan_path():
        return DataReader.data_path("cache", "flag_plan.json")

    @staticmethod
    def flag_plan_grab():
        """The saved group -> flag type plan: {"rows": [...], "run_now",
        "refresh"}."""
        if flagmodule is None:
            return {"rows": [], "run_now": False, "refresh": False}
        return flagmodule.load_plan(path=DataReader.flag_plan_path())

    @staticmethod
    def flag_plan_save(rows):
        """Replace the whole plan. Row order is the plan's meaning - rows apply
        top to bottom and the later one wins - so the list is stored exactly as
        the page sent it. The group name rides along so a row whose group was
        renamed in game can still be labelled (and still matches by name)."""
        if flagmodule is None:
            return False
        clean = []
        for row in rows or []:
            group_id = str((row or {}).get("group_id") or "").strip()
            try:
                flag_type = int((row or {}).get("flag_type"))
            except (TypeError, ValueError):
                continue
            if not group_id or flag_type < 0:
                continue
            clean.append({"group_id": group_id, "flag_type": flag_type,
                          "group_name": str(row.get("group_name") or "")})
        DataReader.ensure_data_dir("cache")
        flagmodule.update_plan(lambda plan: plan.update({"rows": clean}),
                               path=DataReader.flag_plan_path())
        return True

    @staticmethod
    def flag_request(kind):
        """Ask the bot to apply the plan ("run_now") or to re-read the flag
        screen ("refresh") on its next cycle. The bot owns every game request:
        it is the process with the session, the pacing and the captcha
        handling."""
        if kind not in ("run_now", "refresh") or flagmodule is None:
            return False
        DataReader.ensure_data_dir("cache")
        flagmodule.update_plan(lambda plan: plan.update({kind: True}),
                               path=DataReader.flag_plan_path())
        return True

    @staticmethod
    def flag_state_grab():
        """What the bot last read off the flag screen."""
        try:
            path = DataReader.data_path("cache", "flag_state.json")
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f) or {}
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def balancer_state_grab():
        """The resource balancer's own bookkeeping (cache/balancer.json)."""
        try:
            path = DataReader.data_path("cache", "balancer.json")
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f) or {}
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def report_analysis_request(job):
        """Queue one notes job for the bot: the page's selection, written on
        the bot's next cycle and then never again until asked."""
        if reportanalysis is None:
            return False
        DataReader.ensure_data_dir("cache")
        reportanalysis.queue_job(job, path=DataReader.data_path(
            "cache", "report_analysis.json"))
        return True

    @staticmethod
    def report_analysis_state_grab():
        """What the report-analysis pass has already written onto the map."""
        try:
            path = DataReader.data_path("cache", "report_analysis.json")
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f) or {}
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def am_state_grab():
        """What the bot last read off the Account Manager screens."""
        try:
            path = DataReader.data_path("cache", "am_state.json")
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f) or {}
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def snipe_arm_batch(incoming_id, target_village_id, land_ms, options,
                        shortfall, min_pct, boost, max_delta_ms=0):
        """Arm one snipe per selected option: each sends `units` as support
        from its own village so they land at land_ms (epoch milliseconds) in
        the target village. Returns (armed_entries, per_option_errors)."""
        if snipe_engine is None:
            return [], ["snipe engine unavailable"]
        if not (field_distance and unit_travel_seconds):
            return [], ["travel-time helpers unavailable"]
        # Same reason as csnipe_arm: the village being sniped for may be one the
        # bot has not reached yet.
        managed = OverviewBuilder._with_attacked_targets(
            DataReader.cache_grab("managed") or {},
            DataReader.cache_grab("villages") or {},
            OverviewBuilder._build_incomings(
                DataReader.cache_grab("villages") or {}))
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
        # How far off the target landing a command may be and still be kept.
        # 0 means keep whatever lands, which is how this behaved before.
        try:
            max_delta_ms = max(0, int(float(max_delta_ms or 0)))
        except (TypeError, ValueError):
            max_delta_ms = 0
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
                "max_delta_ms": max_delta_ms,
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

    NOBLE_REL = ("cache", "noble_jobs.json")

    @staticmethod
    def noble_path():
        """World-aware path of the noble-job list the bot reads/writes."""
        return DataReader.data_path(*DataReader.NOBLE_REL)

    @staticmethod
    def noble_grab():
        """World-aware read of the noble jobs, enriched for display: current
        loyalty estimate, worst-case nobles still needed and the overshoot
        guard's train size, plus the source village's nobles at home."""
        if noblebarb is None:
            return []
        jobs = noblebarb.load_jobs(path=DataReader.noble_path())
        world = DataReader.cache_grab("world") or {}
        try:
            speed = float((world.get("config") or {}).get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        managed = DataReader.cache_grab("managed") or {}
        # Nobles on their way to each target as the bot last saw them, whoever
        # sent them. The job's own in_flight only covers the bot's own sends,
        # so on its own it under-reports a train launched by hand.
        flying = {}
        flying_age = None
        try:
            with open(DataReader.data_path("cache", "noble_flying.json")) as fh:
                seen = json.load(fh) or {}
            flying = seen.get("targets") or {}
            flying_age = int(time.time()) - int(seen.get("at") or 0)
        except (OSError, ValueError, TypeError):
            pass
        for job in jobs:
            est = noblebarb.estimate_loyalty(job, speed=speed)
            job["loyalty_est"] = est
            job["nobles_needed"] = noblebarb.nobles_needed_worst_case(est)
            job["train_allowed"] = noblebarb.max_safe_nobles(est)
            job["nobles_flying"] = int(flying.get(
                "%s|%s" % (job.get("target_x"), job.get("target_y")), 0) or 0)
            job["flying_age"] = flying_age
            source = managed.get(str(job.get("source_id"))) or {}
            job["nobles_home"] = int(
                (source.get("available_troops") or {}).get("snob", 0) or 0)
        return jobs

    @staticmethod
    def noble_add(target_x, target_y, source_id, escort, escort_min_pct):
        """Add an auto-noble job. The target must be a barbarian village in
        the map cache (that is where its id for report matching and the
        ownership watch come from). Returns (entry, error_message)."""
        if noblebarb is None:
            return None, "noble-barb engine unavailable"
        source_id = str(source_id)
        managed = DataReader.cache_grab("managed")
        origin = managed.get(source_id) or {}
        pub = origin.get("public") or {}
        loc = pub.get("location")
        if not loc or len(loc) != 2:
            return None, "unknown source village"
        try:
            tx, ty = int(target_x), int(target_y)
            escort_min_pct = max(0, min(100, int(float(escort_min_pct))))
        except (TypeError, ValueError):
            return None, "invalid numbers in the form"

        selected = {}
        for unit, count in (escort or {}).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0 and unit != "snob":
                selected[str(unit)] = count

        target = None
        for v in DataReader.cache_grab("villages").values():
            vloc = v.get("location")
            if vloc and len(vloc) == 2 and int(vloc[0]) == tx and int(vloc[1]) == ty:
                target = v
                break
        if not target:
            return None, ("no village known at %d|%d yet - it appears in the "
                          "map cache once the bot has seen that area" % (tx, ty))
        if str(target.get("owner") or "0") != "0":
            return None, "that village is not barbarian (owner %s)" % target.get("owner")
        if str(target.get("id")) in managed:
            return None, "that village is already yours"
        for existing in DataReader.noble_grab():
            if str(existing.get("target_id")) == str(target.get("id")) \
                    and existing.get("status") != "done":
                return None, "there is already a noble job on that village"

        distance = None
        if field_distance:
            distance = round(field_distance((loc[0], loc[1]), (tx, ty)), 1)
        name = target.get("name")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "created": int(time.time()),
            "status": "paused",
            "target_id": str(target.get("id")),
            "target_x": tx, "target_y": ty,
            "target_name": name if isinstance(name, str) and name else "%d|%d" % (tx, ty),
            "source_id": source_id,
            "source_name": origin.get("name") or pub.get("name") or source_id,
            "escort": selected,
            "escort_min_pct": escort_min_pct,
            "distance": distance,
        }
        noblebarb.add_job(entry, path=DataReader.noble_path())
        return entry, None

    @staticmethod
    def noble_toggle(job_id):
        """Arm/pause (or re-arm a stopped) noble job."""
        if noblebarb is None:
            return None
        return noblebarb.toggle_job(str(job_id), path=DataReader.noble_path())

    @staticmethod
    def noble_move(job_id, direction):
        """Shift a job up or down the priority list (see focus_budgets)."""
        if noblebarb is None:
            return False
        return noblebarb.move_job(str(job_id), direction,
                                  path=DataReader.noble_path())

    @staticmethod
    def noble_remove(job_id):
        if noblebarb is None:
            return False
        return noblebarb.remove_job(str(job_id), path=DataReader.noble_path())

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
                # A section added to the bot after this world's config.json was
                # written (e.g. balancer) has no key yet; create it on first save
                # rather than 500-ing on a KeyError.
                if section not in template:
                    template[section] = collections.OrderedDict()
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
    def _bot_user_agent():
        """The user agent the bot browses with, so a request the web process
        makes on its behalf looks like the same client rather than a second one
        appearing on the account from nowhere."""
        try:
            with open(DataReader.data_path("config.json")) as handle:
                return json.load(handle).get("bot", {}).get("user_agent")
        except (OSError, ValueError):
            return None

    @staticmethod
    def world_villages():
        """Every village on the world, from the public map file, cached.

        The bot's own map cache only covers the ground around its villages, so
        an enemy further out is a bare id to it. This is the world's own list
        and it costs one unauthenticated request for all of them.
        """
        if worldvillages is None:
            return {}
        path = DataReader.data_path(*worldvillages.CACHE_REL)
        try:
            if os.path.exists(path) and \
                    time.time() - os.path.getmtime(path) < worldvillages.TTL:
                with open(path) as handle:
                    return json.load(handle) or {}
        except (OSError, ValueError):
            pass
        session = DataReader.get_session() or {}
        fresh = worldvillages.fetch(session.get("endpoint") or "")
        if not fresh:
            # Stale beats nothing: an old ownership reading still names the
            # village, and the alternative is a column of ids.
            try:
                with open(path) as handle:
                    return json.load(handle) or {}
            except (OSError, ValueError):
                return {}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                json.dump(fresh, handle)
        except OSError:
            pass
        return fresh

    @staticmethod
    def world_players():
        """{player_id: name} for the world, from the public map file, cached
        like world_villages() and falling back to a stale copy the same way."""
        if worldvillages is None or not hasattr(worldvillages, "fetch_players"):
            return {}
        path = DataReader.data_path(*worldvillages.PLAYERS_REL)
        try:
            if os.path.exists(path) and \
                    time.time() - os.path.getmtime(path) < worldvillages.TTL:
                with open(path) as handle:
                    return json.load(handle) or {}
        except (OSError, ValueError):
            pass
        session = DataReader.get_session() or {}
        fresh = worldvillages.fetch_players(session.get("endpoint") or "")
        if not fresh:
            try:
                with open(path) as handle:
                    return json.load(handle) or {}
            except (OSError, ValueError):
                return {}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                json.dump(fresh, handle)
        except OSError:
            pass
        return fresh

    @staticmethod
    def dead_clears(min_units=None, min_loss_pct=None, alive_max_loss_pct=None,
                    rebuild_days=None, prefix=None, prefix_alive=None,
                    rebuild_word=None):
        """Every enemy village that has thrown a stack at us, and what became
        of its clear - newest event first, each row carrying "dead" or "alive".

        The scan is the bot module's (game/reportanalysis); this only gathers
        the world-aware caches it reads, so the dashboard and the module can
        never disagree about what counts.
        """
        if reportanalysis is None:
            return []
        ra = reportanalysis
        settings = (DataReader.config_grab() or {}).get("report_analysis") or {}
        if rebuild_days is None:
            rebuild_days = settings.get("rebuild_days", ra.DEFAULT_REBUILD_DAYS)
        rows = reportanalysis.find_clear_states(
            DataReader.cache_grab("reports") or {},
            DataReader.cache_grab("managed") or {},
            DataReader.cache_grab("villages") or {},
            DataReader.world_villages(),
            int(min_units or reportanalysis.DEFAULT_MIN_UNITS),
            int(min_loss_pct if min_loss_pct is not None
                else reportanalysis.DEFAULT_MIN_LOSS_PCT),
            int(alive_max_loss_pct if alive_max_loss_pct is not None
                else reportanalysis.DEFAULT_ALIVE_MAX_LOSS_PCT),
            int(rebuild_days or 0))
        # The line each village would get, worked out here rather than in the
        # page so the page can never write something the bot would not.
        players = DataReader.world_players() if rows else {}
        for row in rows:
            if row.get("owner") and not row.get("owner_name"):
                row["owner_name"] = players.get(str(row["owner"]))
            row["line"] = ra.note_line(
                row,
                prefix if prefix is not None
                else settings.get("note_prefix", ra.DEFAULT_NOTE_PREFIX),
                prefix_alive if prefix_alive is not None
                else settings.get("note_prefix_alive", ra.DEFAULT_NOTE_PREFIX_ALIVE),
                rebuild_word if rebuild_word is not None
                else settings.get("note_rebuild", ra.DEFAULT_NOTE_REBUILD))
        return rows

    @staticmethod
    def village_note_read(village_id):
        """The note currently on a village, plus the token to change it."""
        if villagenotes is None:
            return {"ok": False, "reason": "unavailable"}
        village_id = str(village_id)
        if not village_id.isdigit():
            return {"ok": False, "reason": "bad_village"}
        session = DataReader.get_session() or {}
        home = next(iter(DataReader.cache_grab("managed") or {}), None)
        return villagenotes.read_note(
            village_id, session.get("cookies") or {},
            session.get("endpoint") or "", DataReader._bot_user_agent(),
            home_village=home)

    @staticmethod
    def village_note_add(village_id, line, replace_prefixes=None):
        """Add one line to a village's note without losing what is there.

        Reads first, folds the line in with villagenotes.compose (which returns
        nothing when the line is already present, so running this twice over the
        same reports changes nothing), and only then saves.
        """
        if villagenotes is None:
            return {"ok": False, "reason": "unavailable"}
        current = DataReader.village_note_read(village_id)
        if not current.get("ok"):
            return current
        replace = None
        if replace_prefixes is not None and reportanalysis is not None:
            # An older line of the Report analysis module's own - written with
            # the prefixes the page used, the configured ones or the defaults -
            # is replaced rather than stacked under the new one.
            settings = (DataReader.config_grab() or {}).get("report_analysis") or {}
            replace = reportanalysis.own_line_matcher(
                list(replace_prefixes) + reportanalysis.own_prefixes(settings))
        merged = villagenotes.compose(current.get("note"), line, replace)
        if merged is None:
            if replace is not None:
                DataReader._report_analysis_noted(village_id, line)
            return {"ok": True, "skipped": "already noted",
                    "note": current.get("note")}
        session = DataReader.get_session() or {}
        home = next(iter(DataReader.cache_grab("managed") or {}), None)
        result = villagenotes.write_note(
            str(village_id), merged, session.get("cookies") or {},
            session.get("endpoint") or "", DataReader._bot_user_agent(),
            current.get("csrf"), home_village=home)
        if result.get("ok") and replace is not None:
            DataReader._report_analysis_noted(village_id, line)
        return result

    @staticmethod
    def _report_analysis_noted(village_id, line):
        """Tell the bot's job a Report analysis line is already on a village,
        so it does not spend a page read finding out."""
        try:
            reportanalysis.record_noted(village_id, line, path=DataReader.data_path(
                "cache", "report_analysis.json"))
        except OSError:
            pass

    @staticmethod
    def live_home_troops(village_id):
        """What is standing in a village this second, off the live rally point.

        Every cached figure the forms prefill from is a reading from some
        minutes ago; this is the one the send itself would get. Returns the
        reader's status dict ({"ok": True, "units": {...}} or a reason), never
        raises, and writes nothing.
        """
        if read_home_troops is None:
            return {"ok": False, "reason": "unavailable"}
        village_id = str(village_id)
        if not village_id.isdigit():
            return {"ok": False, "reason": "bad_village"}
        session = DataReader.get_session() or {}
        return read_home_troops(village_id,
                                session.get("cookies") or {},
                                session.get("endpoint") or "",
                                DataReader._bot_user_agent())

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
        user_agent = DataReader._bot_user_agent()
        # Load the captured rename endpoint world-aware: the game module's own
        # load_label_endpoint resolves against the bot's FileManager data root,
        # which the web process does not have, so it would read the default
        # world's cache and report 'no_endpoint_yet' on --world setups.
        label_cfg = None
        try:
            cache_path = DataReader.data_path("cache", "world", "incoming_label.json")
            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    label_cfg = json.load(f)
        except (OSError, ValueError):
            pass
        return rename_command_ingame(command_id, label, cookies, endpoint,
                                     user_agent, label_cfg=label_cfg)

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

        cache_dir = DataReader.ensure_data_dir("cache")
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
        cache_dir = DataReader.ensure_data_dir("cache")
        with open(os.path.join(cache_dir, "portal_cookies.json"), 'w') as f:
            json.dump({"domain": DataReader.portal_domain(), "cookies": cookies}, f, indent=2)
        return True

    @staticmethod
    def portal_domain(host=None):
        """The account portal host for a world, e.g. www.tribalwars.nl.

        Worlds live on <world>.tribalwars.<tld> and the portal that owns the
        login cookies is www.tribalwars.<tld>, so derive it from the game
        endpoint instead of hardcoding one market. Hardcoding .nl silently
        broke session restore everywhere else: the portal cookies got injected
        onto a domain the player never visits.

        Pass a game hostname, or leave it out to use the active world's.
        """
        if host is None:
            endpoint = (DataReader.get_session() or {}).get("endpoint") or ""
            # Cheap parse - the endpoint is always scheme://host/path.
            host = endpoint.split("://")[-1].split("/")[0]
        parts = [p for p in (host or "").split(".") if p]
        if len(parts) >= 3:
            return "www." + ".".join(parts[1:])
        return "www.tribalwars.nl"

    @staticmethod
    def portal_cookies_get():
        p = DataReader.data_path("cache", "portal_cookies.json")
        if not os.path.exists(p):
            # Portal cookies are account-level (www.tribalwars.nl), not
            # world-level, fall back to the newest copy from any world.
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
    """Everything the map page needs to draw the world itself.

    It used to hand over a fixed grid of cells centred on one village, which is
    why the map could not be moved: there was nothing outside the window to move
    to. The whole village database is a few hundred kilobytes, so it goes across
    as a flat list and the page draws whatever part of the world it is looking
    at - which is what makes panning, zooming and jumping to a coordinate
    possible at all.
    """

    @staticmethod
    def build(villages, mine=None, current_village=None):
        mine = mine or {}
        # The player's own in-game map markings, if the bot has read them.
        # Colouring the dashboard's map differently from the game's would mean
        # learning the same world twice.
        marks = (DataReader.cache_grab("world") or {}).get("markings") or {}
        by_tribe = marks.get("tribes") or {}
        by_player = marks.get("players") or {}
        owner = tribe = None
        centre = None

        out = []
        for vid, village in (villages or {}).items():
            location = village.get("location")
            if not location or len(location) != 2:
                continue
            is_mine = str(vid) in mine
            if is_mine:
                # Ownership is read off our own villages rather than assumed, so
                # a village of ours identifies the player and the tribe even
                # when the map was opened without a village to centre on.
                owner = owner or village.get("owner")
                tribe = tribe or village.get("tribe")
            if current_village and str(vid) == str(current_village):
                centre = [int(location[0]), int(location[1])]
            marked = (by_player.get(str(village.get("owner")))
                      or by_tribe.get(str(village.get("tribe"))))
            out.append({
                "id": str(vid),
                "name": village.get("name") or str(vid),
                "color": marked,
                "x": int(location[0]),
                "y": int(location[1]),
                "points": village.get("points") or 0,
                "tribe": str(village.get("tribe") or "0"),
                "owner": str(village.get("owner") or "0"),
                "mine": is_mine,
            })

        ours = [(v["x"], v["y"]) for v in out if v["mine"]]
        if centre is None and ours:
            # The middle of our own villages is the only sensible opening view:
            # it is where the account actually is.
            centre = [round(sum(x for x, _ in ours) / len(ours)),
                      round(sum(y for _, y in ours) / len(ours))]
        return {
            "villages": out,
            "marks": {"labels": marks.get("labels") or {},
                      "tribes": by_tribe, "players": by_player},
            "centre": centre or [500, 500],
            "owner": str(owner or "0"),
            "tribe": str(tribe or "0"),
            "mine_count": len(ours),
        }


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
        return (rpath,) + DataReader._dir_signature(rpath)

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
    def _active_bounds(spec):
        """(start_minute, end_minute) of the bot's active_hours window, or None
        when it is unset/malformed (= always active).

        Mirrors twb.py is_active_hours, HH:MM bounds included: the old
        int()-only parser could not read "5-23:30" and quietly answered "always
        active" for every world configured that way.
        """
        if not spec:
            return None

        def to_minutes(bound, is_end):
            bound = bound.strip()
            if ":" in bound:
                hour, minute = bound.split(":")
                return int(hour) * 60 + int(minute)
            # A whole-hour end bound is inclusive ("6-23" runs to 23:59).
            return int(bound) * 60 + (59 if is_end else 0)

        try:
            raw_start, raw_end = str(spec).split("-")
            return to_minutes(raw_start, is_end=False), to_minutes(raw_end, is_end=True)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _in_active_hours(cls, spec):
        """True if the current local time falls in the bot's active_hours window."""
        bounds = cls._active_bounds(spec)
        if not bounds:
            return True
        start, end = bounds
        now = time.localtime()
        now_m = now.tm_hour * 60 + now.tm_min
        if start <= end:
            return start <= now_m <= end
        # Overnight window that wraps past midnight (e.g. "22-6").
        return now_m >= start or now_m <= end

    @classmethod
    def _active_seconds_between(cls, since, until, spec):
        """How many of the seconds in [since, until] fell inside active_hours.

        The farm-stall clock has to run on this rather than wall time: with a
        nightly pause the newest combat report is hours old every morning
        through no fault of the bot's.
        """
        bounds = cls._active_bounds(spec)
        if not bounds:
            return max(0, int(until - since))
        start, end = bounds
        # Walk the window in 5-minute steps: coarse enough to stay cheap over a
        # multi-day gap, fine enough for a threshold measured in hours.
        step = 300
        active = 0
        t = int(since)
        while t < until:
            lt = time.localtime(t)
            minute = lt.tm_hour * 60 + lt.tm_min
            inside = start <= minute <= end if start <= end else (minute >= start or minute <= end)
            if inside:
                active += min(step, int(until) - t)
            t += step
        return active

    @classmethod
    def _farm_stall_state(cls, newest_combat_ts, watchdog):
        """Detect the 'silent stall': the main loop is alive during active hours
        and farming is on, but no attack/scout report has been ingested for
        FARM_STALL_SECONDS. This is the classic silent-stall signature - a degraded session that
        keeps the loop turning (fresh heartbeat) while report reading is dead.

        The age is only evidence against the bot for the stretch it was actually
        up and farming, so two stretches are excluded:

        - time before this process started. After any multi-hour outage the
          newest combat report is old for reasons that have nothing to do with
          report reading, and the banner used to accuse a freshly restarted bot
          of the very thing the restart just fixed (seen 2026-08-08, right after
          a 4h quest-recursion outage: attacks were already flying again while
          the dashboard still cried stall).
        - time outside active_hours, where farming is paused by design and no
          report can arrive - otherwise every morning after a nightly pause
          looks like a stall.

        Note this only delays a genuine stall's detection: nothing can be
        concluded until the bot has been up and farming for the full window.
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
        active_hours = (cfg.get("bot", {}) or {}).get("active_hours")
        # ...and during active hours, when the bot should be actively farming.
        if not cls._in_active_hours(active_hours):
            return idle
        # No baseline yet (fresh world, no attack/scout reports) -> don't cry wolf.
        if not newest_combat_ts:
            return idle
        now = int(time.time())
        age = now - int(newest_combat_ts)
        # Only hold the bot responsible from the moment it came up.
        started = cls._to_int(watchdog.get("started"))
        blame_from = max(int(newest_combat_ts), started)
        blamed = cls._active_seconds_between(blame_from, now, active_hours)
        if blamed > cls.FARM_STALL_SECONDS:
            return {"stalled": True, "since": int(newest_combat_ts), "age": age}
        return idle

    @staticmethod
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _arrival_key(command):
        """Sort key for an incoming or support command: its arrival to the ms.

        A train lands several commands inside the same second, so ordering on
        `arrival` alone leaves them in whatever order the cache happened to
        hand over - and which of them lands first is the entire question when
        you are picking a gap to snipe into. `arrival_ms` is the ms-exact
        reading the poller takes off the arrival column; when it has not read
        one yet the whole second is all we know, so it sorts as .000 and sits
        ahead of anything in the same second whose millisecond IS known.
        """
        millis = command.get("arrival_ms")
        if millis:
            try:
                return int(millis)
            except (TypeError, ValueError):
                pass
        return OverviewBuilder._to_int(command.get("arrival")) * 1000

    @staticmethod
    def _with_attacked_targets(managed, village_db, incomings_by_target):
        """`managed`, plus any village the poller sees attacks on that the bot
        has not run yet.

        A village only gets a managed snapshot when the bot's cycle reaches it,
        and on a big account that is hours after it is conquered. Everything on
        the Defense page is keyed off those snapshots, so a village taken this
        afternoon was invisible on it while three attacks walked towards it -
        precisely the case the page exists for, missed for the least
        interesting possible reason.

        The map cache knows the village (it is on the map whoever owns it), so
        its name and coordinates stand in until the bot writes a real snapshot.
        No troops are claimed for it: an empty available_troops reads as "not
        known", which is the truth, rather than as a garrison.
        """
        if not incomings_by_target:
            return managed
        extra = {}
        for target_id, commands in incomings_by_target.items():
            if str(target_id) in managed or not commands:
                continue
            known = village_db.get(str(target_id)) or {}
            extra[str(target_id)] = {
                "name": known.get("name") or str(target_id),
                "public": {"location": known.get("location"),
                           "points": known.get("points")},
                "available_troops": {},
                "troops": {},
                "building_queue": [],
                # The dashboard's village table reads these per resource, so
                # they are present and zero rather than absent: nothing is known
                # about the village yet, and a missing key is a render error
                # while a zero is merely an empty-looking row.
                "resources": {"wood": 0, "stone": 0, "iron": 0, "pop": 0},
                # Read by the page to explain the missing garrison rather than
                # showing the village as empty.
                "not_run_yet": True,
            }
        if not extra:
            return managed
        merged = dict(managed)
        merged.update(extra)
        return merged

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
            commands.sort(key=cls._arrival_key)
        return by_target

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        attacks = data.get("attacks", {}) or {}
        reports = data.get("reports", {}) or {}
        incomings_by_target = cls._build_incomings(data.get("villages", {}) or {})
        # Same blind spot as the Defense page had: the "under attack now" card
        # is built by walking the villages the bot has run, so attacks on one it
        # has not reached yet never reached the card either.
        managed = cls._with_attacked_targets(
            managed, data.get("villages", {}) or {}, incomings_by_target)

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

            # Warehouse pressure, measured on the fullest resource: production
            # of that one is being thrown away long before the other two catch
            # up, so an average would hide exactly the villages worth looking at.
            capacity = cls._to_int(vdata.get("storage_max"))
            fullest = max(cls._to_int(resources.get(k)) for k in ("wood", "stone", "iron"))

            villages.append({
                "id": vid,
                "name": name,
                "points": public.get("points"),
                "location": public.get("location"),
                "resources": resources,
                "storage_max": capacity,
                "storage_pct": min(100, round(fullest * 100 / capacity)) if capacity else None,
                "troops": {u: a for u, a in available.items() if cls._to_int(a) > 0},
                "queue_len": len(queue),
                # What the game's own HQ queue is building right now, which is
                # what the villages table shows - the plan above can be a
                # hundred entries long and says nothing about what is underway.
                "queue_ingame": parse_hq_queue(vdata.get("building_queue_ingame")),
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

        # Where the account's units actually stand. The account-wide
        # type=complete overview (cache/troops_moving.json, refreshed every few
        # minutes by the bot) is the only reading that knows: a village's own
        # units page shows what is standing in the village and nothing about
        # what it has out scavenging or parked in another village. Summing the
        # per-village snapshots is therefore "at home right now", never a total
        # - with the defensive stacks out on a gather run that reads a fraction
        # of the real army, which is exactly how a 34k spear account renders as
        # 16k. `home_troops` below is that snapshot sum; keep it separate.
        snapshot_home = dict(home_troops)
        cache = DataReader.troop_locations()
        if not isinstance(cache, dict):
            cache = {}
        troops_when = cls._to_int(cache.get("complete_when")) or cls._to_int(cache.get("when"))
        troops_age = (int(time.time()) - troops_when) if troops_when else None
        troops_moving = {u: cls._to_int(c) for u, c in (cache.get("moving", {}) or {}).items()
                         if cls._to_int(c)}
        troops_support = {u: cls._to_int(c) for u, c in (cache.get("support", {}) or {}).items()
                          if cls._to_int(c)}

        if cache.get("by_village"):
            # Complete reading: home, support and moving all come off the same
            # page, so every column is consistent and Total is exactly the sum.
            home_troops = {u: cls._to_int(c) for u, c in (cache.get("home", {}) or {}).items()
                           if cls._to_int(c)}
            total_troops = {}
            for part in (home_troops, troops_support, troops_moving):
                for unit, count in part.items():
                    total_troops[unit] = total_troops.get(unit, 0) + count
            troops_partial = bool(cache.get("partial"))
        else:
            # Degraded reading: the complete table would not parse, or nothing
            # is cached yet (first cycle). Every estimate left is a lower bound
            # taken at its own moment - the per-village totals cannot see units
            # parked in another village, the away pools are only what is out,
            # the home snapshot only what is in. Take the largest per unit.
            # Never ADD across them: the village snapshots are minutes apart, so
            # home + moving counts the same gather squad twice, once where it
            # stood and once where it is now. Flagged partial either way.
            troops_partial = True
            estimates = (total_troops, snapshot_home,
                         {u: troops_support.get(u, 0) + troops_moving.get(u, 0)
                          for u in set(troops_support) | set(troops_moving)})
            units_seen = set().union(*(set(e) for e in estimates))
            total_troops = {u: max(e.get(u, 0) for e in estimates) for u in units_seen}

        # Away is never derived by subtracting two snapshots taken at different
        # moments - that leaves phantom troops. It is the two away pools added.
        troops_away = {u: troops_support.get(u, 0) + troops_moving.get(u, 0)
                       for u in total_troops}

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
        # now, across all villages), not the bot's planned build order.
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
                # Age of the troop-location reading the four columns come from,
                # and whether it was complete. The panel shows both: a silently
                # stale or half-parsed split is the one failure mode that makes
                # these numbers look like missing units.
                "troops_age": troops_age,
                "troops_partial": troops_partial,
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


class PlanImport:
    """Review data for a pasted attack plan (devilicious.dev and friends).

    Matches every planned line to one of our villages, re-derives the travel
    time from this world's own unit speeds, and flags whatever would go wrong -
    an origin that is not ours, a send moment already past, a plan built for
    different world settings, two commands emptying the same village. Nothing
    is queued here: the Attack tab shows this table first and only sends what
    the user then confirms.
    """

    # What an attack is made of, in the order the units field reads best. A
    # plan only ever names the unit it paced a command with, so a nuke row used
    # to arrive here as "ram: all" and had to be typed out by hand every time.
    # Defensive units stay home and nobles get their own row; the paladin rides
    # along because "all" costs nothing in a village that has none - the rally
    # point resolves it to what is standing there and drops the units that are
    # not, so the command still goes out.
    OFF_UNITS = ["axe", "light", "marcher", "knight", "spy", "ram", "catapult"]

    @classmethod
    def _suggest_units(cls, row, speeds=None):
        """A starting point for the troops, which no plan actually specifies.

        Only units at least as fast as the one the plan paced the command with,
        so the send moment the plan calculated still holds. That is what makes
        catapults free in a ram-paced nuke: they walk at exactly ram pace, so
        they ride along without the command having to leave a second earlier.
        An axe-paced row gets no siege for the same reason - rams and catapults
        would double its travel time and throw the plan's send moment out.

        Fakes are left alone: the plan already says what they are, and a fake
        is a handful of units, not an army.
        """
        if row["unit"] == "snob":
            return {"snob": row["count"], "axe": "all", "light": "all"}
        pace = (speeds or {}).get(row["unit"])
        if (row["kind"] != "attack" or row["kind_raw"].lower() == "fake"
                or row["unit"] not in cls.OFF_UNITS or not pace):
            return {row["unit"]: "all"}
        return {unit: "all" for unit in cls.OFF_UNITS
                if speeds.get(unit) and speeds[unit] <= pace}

    @classmethod
    def build(cls, text):
        rows, skipped = attack_plan.parse_plan(text)

        managed = DataReader.cache_grab("managed")
        mine = {}
        for vid, village in managed.items():
            loc = (village.get("public") or {}).get("location")
            if loc and len(loc) == 2:
                mine[(int(loc[0]), int(loc[1]))] = (
                    str(vid), village.get("name") or str(vid))

        named = {}
        for village in DataReader.cache_grab("villages").values():
            loc = village.get("location")
            name = village.get("name")
            if loc and len(loc) == 2 and isinstance(name, str) and name:
                named[(int(loc[0]), int(loc[1]))] = name

        ws, us, speeds = DataReader.world_speeds()
        now = time.time()
        seen_origins = {}
        out = []
        for row in rows:
            entry = dict(row)
            # Blocking problems are ones we cannot send through at all; warnings
            # are judgement calls the user may well have reasons for, so those
            # rows stay tickable, just not ticked for them.
            problems, warnings = [], []

            origin = mine.get(tuple(row["origin"]))
            entry["origin_id"], entry["origin_name"] = origin or (None, None)
            if not origin:
                problems.append("%d|%d is not one of your villages"
                                % tuple(row["origin"]))
            entry["target_name"] = named.get(tuple(row["target"]))

            speed = speeds.get(row["unit"])
            if speed and field_distance and unit_travel_seconds:
                travel = unit_travel_seconds(
                    field_distance(row["origin"], row["target"]), speed, ws, us)
                entry["travel_seconds"] = int(travel)
                # The plan's own travel column, for comparison: a mismatch means
                # it was built against different world speeds, and every send
                # moment in it is wrong for this world.
                entry["travel_delta"] = int(round(travel - row["plan_travel_seconds"]))
                entry["send_ts"] = row["arrival_ts"] - travel
                if abs(entry["travel_delta"]) > 60:
                    warnings.append("plan's travel time is off by %+ds for this "
                                    "world - check the plan's speed settings"
                                    % entry["travel_delta"])
                if entry["send_ts"] <= now:
                    problems.append("would have to leave %s ago"
                                    % _short_duration(now - entry["send_ts"]))
            else:
                problems.append("no travel speed for '%s' on this world" % row["unit"])

            if origin:
                first = seen_origins.get(origin[0])
                if first:
                    warnings.append("village also sends on line %d - give each "
                                    "command its own counts, not 'all'" % first)
                else:
                    seen_origins[origin[0]] = row["line"]

            entry["units"] = cls._suggest_units(row, speeds)
            entry["train"] = (row["unit"] == "snob" and row["count"] > 1)
            entry["problems"] = problems
            entry["warnings"] = warnings
            entry["ok"] = not problems
            # Only pre-tick what is both sendable and unremarkable.
            entry["selected"] = not problems and not warnings
            out.append(entry)

        # Every origin unknown, on a world that does have villages, is almost
        # never a broken plan - it is a plan for another world, read while the
        # dashboard was pointed at this one. Say that once, rather than leaving
        # the same per-row error repeated down the table to be interpreted.
        wrong_world = bool(out) and mine and not any(r["origin_id"] for r in out)

        return {
            "rows": out,
            "skipped": skipped,
            "wrong_world": wrong_world,
            "queueable": sum(1 for r in out if r["ok"]),
            # Plan timestamps are read on the host clock; say how far that is
            # from the game server so an off-by-minutes host is visible here.
            "clock_offset": round(DataReader.server_clock_offset(), 1),
        }


# Farm space each unit takes, for telling a fake from a real attack (and for
# the minimum-attack-size rule some worlds enforce).
UNIT_POP = {"spear": 1, "sword": 1, "axe": 1, "archer": 1, "spy": 2, "light": 4,
            "marcher": 5, "heavy": 6, "ram": 5, "catapult": 8, "knight": 10,
            "snob": 100}
# Below this much population a command is a fake, not an attack: a real nuke is
# thousands, a fake is a catapult and some scouts.
FAKE_POP = 1000


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
                # Troops as of the bot's last visit to this village, which on a
                # 40-village account is up to an hour ago. That is fine for
                # filling a form and misleading for judging what can be sent, so
                # the age travels with the number and is shown wherever the
                # count is used to warn about anything.
                "troops": {u: int(n) for u, n in (vdata.get("available_troops") or {}).items()},
                "seen": vdata.get("last_run"),
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
            # Minimum attack size this world enforces, as a percentage of the
            # sending village's points. The rally point refuses anything under
            # it at the moment of launch, so the page warns while there is still
            # time to fix it. 0 when the world has no such rule.
            "fake_limit": float((DataReader.cache_grab("world") or {})
                                .get("config", {}).get("fake_limit") or 0),
            # The player's own rally-point templates ("OFF", "Fake", ...), for
            # filling the unit fields without retyping them here.
            "templates": DataReader.troop_templates(),
            "now": int(time.time()),
        }

    @staticmethod
    def operations(data, scheduled):
        """What is in the air right now, which is what the page is opened for.

        The tracked-target list answers "what could I hit"; it is hundreds of
        rows long and changes slowly. Since the scheduler took over sending, the
        question that actually needs answering on arrival is "what have I got
        out there" - commands already launched and still flying, commands still
        waiting to leave, and where the nobles are. None of that was anywhere.
        """
        now = time.time()
        managed = data.get("bot", {}) or {}
        moves = DataReader.troop_locations()
        by_village = moves.get("by_village") or {}

        def describe(command):
            """A word for what this command is, from what it carries.

            Size first, because it is what separates a fake from the real
            thing: a fake is a catapult and a handful of scouts, and calling
            that "siege" because it holds a catapult reads as a threat it is
            not."""
            if command.get("waves"):
                return "%d-noble train" % len(command["waves"])
            units = command.get("units") or {}
            if units.get("snob"):
                return "noble"
            def count(unit):
                try:
                    return int(units.get(unit))
                except (TypeError, ValueError):
                    return 0
            # "all" is only a number at send time, and it is never a fake -
            # nobody fakes with everything the village has.
            vague = any(str(n).strip().lower() == "all" for n in units.values())
            pop = sum(UNIT_POP.get(u, 1) * count(u) for u in units)
            if not vague and pop and pop < FAKE_POP:
                return "fake"
            if count("ram") + count("catapult") >= 50:
                return "siege"
            if units.get("axe") or units.get("light") or units.get("archer"):
                return "nuke"
            return "attack"

        in_flight, queued = [], []
        for command in scheduled:
            row = {
                "id": command.get("id"),
                "target": "%s|%s" % (command.get("target_x"), command.get("target_y")),
                "target_name": command.get("target_name"),
                "origin": command.get("origin_name"),
                "what": describe(command),
                "arrival_ts": command.get("arrival_ts"),
                "send_ts": command.get("send_ts"),
            }
            status = command.get("status")
            if status == "sent" and (command.get("arrival_ts") or 0) > now:
                in_flight.append(row)
            elif status in ("pending", "sending"):
                queued.append(row)
        in_flight.sort(key=lambda r: r["arrival_ts"] or 0)
        queued.sort(key=lambda r: r["send_ts"] or 0)

        targets = {}
        for row in in_flight:
            targets[row["target"]] = targets.get(row["target"], 0) + 1

        return {
            "in_flight": in_flight,
            "queued": queued,
            "targets_hit": targets,
            "nobles_home": sum(int((v.get("available_troops") or {}).get("snob") or 0)
                               for v in managed.values()),
            "nobles_away": int((moves.get("moving") or {}).get("snob") or 0),
            "villages_out": sum(1 for v in by_village.values()
                                if any((v.get("moving") or {}).values())),
            "villages": len(by_village) or len(managed),
            "moving": moves.get("moving") or {},
            "seen": moves.get("when"),
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
        # A village under attack belongs on this page whether or not the bot has
        # got round to running it yet.
        managed = OverviewBuilder._with_attacked_targets(
            managed, village_db, incomings_by_target)
        now = int(time.time())
        # Incoming support, cached whole by the poller. Deliberately kept out of
        # incomings_by_target: everything downstream of that decides whether a
        # village is under attack, and a reinforcement is not an attack.
        supports_by_target = {}
        support_age = None
        try:
            path = DataReader.data_path("cache", "incoming_support.json")
            blob = {}
            if os.path.exists(path):
                with open(path) as handle:
                    blob = json.load(handle) or {}
            when = OverviewBuilder._to_int(blob.get("when"))
            support_age = (now - when) if when else None
            for command in (blob.get("commands") or []):
                # The scraper records the arrival; the countdown is derived
                # here, the same way the attack side does it, so a cache read
                # minutes ago still counts down correctly.
                arrival = OverviewBuilder._to_int(command.get("arrival"))
                if not arrival:
                    continue
                eta = arrival - now
                if eta <= 0:
                    continue        # already landed
                command = dict(command, eta=eta)
                supports_by_target.setdefault(
                    str(command.get("target_id")), []).append(command)
        except Exception:
            supports_by_target = {}
        for cmds in supports_by_target.values():
            cmds.sort(key=OverviewBuilder._arrival_key)

        # Garrisons come from the account-wide troop-location reading the bot
        # refreshes every few minutes (cache/troops_moving.json), NOT from each
        # village's cached available_troops. That snapshot is only rewritten
        # when the bot next runs the village, so a stack that left on a gather
        # run half an hour ago still reads as sitting at home - the page then
        # promises defence that is nowhere near the wall.
        locations = DataReader.troop_locations()
        if not isinstance(locations, dict):
            locations = {}
        by_village = locations.get("by_village") or {}
        # A partial write carries the previous by_village breakdown forward, so
        # age the garrison figures by when that breakdown was actually read.
        reading_when = (OverviewBuilder._to_int(locations.get("complete_when"))
                        or OverviewBuilder._to_int(locations.get("when")))
        reading_age = (now - reading_when) if reading_when else None

        villages = []
        total_def = {u: 0 for u in cls.DEFENSIVE_UNITS}
        total_def_away = {u: 0 for u in cls.DEFENSIVE_UNITS}
        stale_villages = 0
        under_attack_count = 0
        total_incoming = 0

        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            located = by_village.get(str(vid))
            if located:
                # "own" is this village's own units standing in it right now;
                # "elsewhere"/"moving" are the same village's units parked in
                # another village or in transit (gather runs included).
                own = located.get("own") or {}
                home_def = {u: OverviewBuilder._to_int(own.get(u)) for u in cls.DEFENSIVE_UNITS}
                away_def = {
                    u: OverviewBuilder._to_int((located.get("elsewhere") or {}).get(u))
                       + OverviewBuilder._to_int((located.get("moving") or {}).get(u))
                    for u in cls.DEFENSIVE_UNITS
                }
                fresh = True
            else:
                # No live reading for this village (first cycle, or the overview
                # would not parse). Fall back to its own snapshot and say so
                # rather than showing it as an empty village.
                avail = vdata.get("available_troops", {}) or {}
                home_def = {u: OverviewBuilder._to_int(avail.get(u)) for u in cls.DEFENSIVE_UNITS}
                away_def = {u: 0 for u in cls.DEFENSIVE_UNITS}
                fresh = False
                stale_villages += 1

            for u in cls.DEFENSIVE_UNITS:
                total_def[u] += home_def[u]
                total_def_away[u] += away_def[u]

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
                "supports": supports_by_target.get(str(vid), []),
                "def_troops": home_def,
                "def_total": sum(home_def.values()),
                "def_away": away_def,
                "def_away_total": sum(away_def.values()),
                "def_fresh": fresh,
                # Under attack, but the bot's cycle has not reached it yet, so
                # everything except the incomings themselves is unknown.
                "not_run_yet": bool(vdata.get("not_run_yet")),
            })

        # Under-attack villages first (soonest arrival first), then strongest garrisons.
        villages.sort(key=lambda v: (
            0 if v["incoming"] else 1,
            v["soonest_eta"] if v["soonest_eta"] is not None else 1 << 62,
            -v["def_total"],
        ))

        # The in-game groups, so the table can be cut down the way the account
        # is already organised - "show me the front" rather than scrolling 58
        # rows. Only groups that actually hold a managed village are offered.
        groups = DataReader.groups_grab()
        here = {str(v["id"]) for v in villages}
        group_of = {}
        group_options = []
        for group in groups:
            members = [str(m) for m in (group.get("villages") or [])]
            held = [m for m in members if m in here]
            if not held:
                continue
            group_options.append({"id": str(group.get("id")),
                                  "name": group.get("name"),
                                  "type": group.get("type"),
                                  "count": len(held)})
            for m in held:
                group_of.setdefault(m, []).append(str(group.get("id")))
        for v in villages:
            v["groups"] = group_of.get(str(v["id"]), [])

        # Every distinct in-game command name currently inbound, commonest
        # first. "Aanval" is what an untagged attack is called, so this doubles
        # as "how many are still waiting for a tag".
        label_counts = collections.Counter(
            (c.get("game_label") or "").strip()
            for cmds in incomings_by_target.values() for c in cmds
            if (c.get("eta") or 0) > 0 and (c.get("game_label") or "").strip())
        incoming_labels = [{"name": name, "count": n}
                           for name, n in label_counts.most_common()]

        return {
            "villages": villages,
            "groups": group_options,
            "incoming_labels": incoming_labels,
            # Each command carries an eta, not an absolute time; the page turns
            # them back into arrivals against this.
            "now": now,
            "support_age": support_age,
            "defensive_units": cls.DEFENSIVE_UNITS,
            "total_def": total_def,
            "total_def_sum": sum(total_def.values()),
            # What is out (supporting elsewhere or in transit) and will not be
            # home for an incoming unless it is recalled in time.
            "total_def_away": total_def_away,
            "total_def_away_sum": sum(total_def_away.values()),
            # Provenance of the garrison figures, so a stale or partial reading
            # is visible instead of being read as "this is what I have".
            "reading_age": reading_age,
            "reading_live": bool(by_village),
            "stale_villages": stale_villages,
            "under_attack_count": under_attack_count,
            "total_incoming": total_incoming,
            "village_count": len(managed),
        }


def live_incomings(managed, village_db):
    """Future incoming attacks as dashboard rows, soonest arrival first
    (shared by the C-snipe and Snipe tabs)."""
    incomings_by_target = OverviewBuilder._build_incomings(village_db)
    managed = OverviewBuilder._with_attacked_targets(
        managed, village_db, incomings_by_target)
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
    incomings.sort(key=OverviewBuilder._arrival_key)
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
    def _suggest_target(loc, candidates, speeds, world_speed, unit_speed,
                        lead_seconds, kind):
        """Nearest of `candidates` whose travel time keeps the troops under way
        at the cancel moment even for a pure-heavy send; slower units only add
        margin. `kind` is the command such a target takes ("attack" for a
        barbarian, "support" for one of your own), which the form needs because
        the game refuses the other one outright."""
        if not (field_distance and unit_travel_seconds) or not loc:
            return None
        base = speeds.get("heavy") or 11
        required = lead_seconds / 2.0 + 180
        best = None
        for candidate in candidates:
            distance = field_distance(loc, candidate["location"])
            travel = unit_travel_seconds(distance, base, world_speed, unit_speed)
            if travel < required:
                continue
            if best is None or distance < best["distance"]:
                best = {
                    "x": int(candidate["location"][0]),
                    "y": int(candidate["location"][1]),
                    "name": candidate.get("name"),
                    "kind": kind,
                    "support": kind == "support",
                    "distance": round(distance, 1),
                    "travel_min_heavy": int(travel // 60),
                }
        return best

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}
        now = int(time.time())

        # A village under attack has to be in here, whether or not the bot has
        # run it yet: this map is what the arm button looks the village up in,
        # and a village missing from it made the button do nothing at all.
        managed = OverviewBuilder._with_attacked_targets(
            managed, village_db, OverviewBuilder._build_incomings(village_db))

        world_cfg = (DataReader.cache_grab("world") or {}).get("config") or {}
        cancel_seconds = int(world_cfg.get("command_cancel_time") or 600)
        ws, us, speeds = DataReader.world_speeds()
        barbs = [
            v for v in village_db.values()
            if str(v.get("owner")) == "0"
            and isinstance(v.get("location"), list) and len(v["location"]) == 2
        ]

        # Garrisons from the account-wide troop-location reading the bot
        # refreshes every few minutes, not from each village's own
        # available_troops snapshot: that snapshot is only rewritten when the
        # bot next runs the village, so it is routinely an hour old and is
        # simply EMPTY for a village the bot has not run yet - which is how a
        # village holding a stack came up as "/0" on every unit in this form.
        home_reading, reading_age = DataReader.home_troop_reading()

        own_villages = []
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            oloc = pub.get("location")
            if isinstance(oloc, list) and len(oloc) == 2:
                own_villages.append({
                    "id": str(vid), "location": oloc,
                    "name": vdata.get("name") or pub.get("name") or str(vid)})

        villages = {}
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            loc = pub.get("location")
            avail = vdata.get("available_troops", {}) or {}
            live = home_reading.get(str(vid))
            source = live if live is not None else avail
            name = vdata.get("name") or pub.get("name") or vid
            lead = cls.DEFAULT_LEAD_MIN * 60
            villages[str(vid)] = {
                "id": str(vid),
                "name": name,
                "coords": loc,
                "home": {u: OverviewBuilder._to_int(source.get(u))
                         for u in cls.FORM_UNITS},
                # Whether that came from the live reading or from the village's
                # own stale snapshot, so the form can say which it is showing.
                "home_fresh": live is not None,
                "barb": cls._suggest_target(loc, barbs, speeds, ws, us,
                                            lead, "attack"),
                # Somewhere to send that is not a fight. A cancel that never
                # goes through throws the stack at the barb, and a grown barb
                # defends; support that fails to cancel just parks it in your
                # own village, alive, until you recall it. Offered always, not
                # only when there is no barb - which of the two failure modes
                # you want is the player's call, not the map's.
                "friendly": cls._suggest_target(
                    loc, [v for v in own_villages if v["id"] != str(vid)],
                    speeds, ws, us, lead, "support"),
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
            # How old the prefilled troop counts are, so the form can say so
            # instead of presenting a reading from ten minutes ago as fact.
            "home_age": reading_age,
            "now": now,
        }


class SnipeOverview:
    """View-model for the Defense page's Snipe tab: incoming attacks, every
    managed village's defensive troops at home + coordinates, the cached
    in-game village groups and the world speeds - the tab computes the
    per-village/per-pace snipe options client-side from these, Toxic-Donut
    style, and arms the checked ones in one go.
    """

    # Every unit that can walk as support - off units included, so a ram-pace
    # or full-off snipe is possible. Militia never travels and nobles are
    # never risked. DEFAULT_UNITS is what starts ticked in the options panel:
    # the classic defensive snipe set.
    SNIPE_UNITS = ["spear", "sword", "axe", "archer", "spy", "light",
                   "marcher", "heavy", "knight", "ram", "catapult"]
    DEFAULT_UNITS = ["spear", "sword", "archer", "marcher", "heavy", "knight"]

    DEFAULT_OFFSET_MS = snipe_engine.DEFAULT_OFFSET_MS if snipe_engine else -100
    DEFAULT_MIN_PCT = snipe_engine.DEFAULT_MIN_PCT if snipe_engine else 80

    @classmethod
    def build(cls, data):
        managed = data.get("bot", {}) or {}
        village_db = data.get("villages", {}) or {}

        # As in the c-snipe tab: the village the options are being computed FOR
        # has to be in this map or the button that opens them does nothing.
        managed = OverviewBuilder._with_attacked_targets(
            managed, village_db, OverviewBuilder._build_incomings(village_db))

        # Same live reading the Defense table and the c-snipe form use; a
        # village's own available_troops snapshot is too old to decide which
        # villages can still reach a landing.
        home_reading, reading_age = DataReader.home_troop_reading()

        villages = {}
        for vid, vdata in managed.items():
            pub = vdata.get("public", {}) or {}
            avail = vdata.get("available_troops", {}) or {}
            live = home_reading.get(str(vid))
            source = live if live is not None else avail
            villages[str(vid)] = {
                "id": str(vid),
                "name": vdata.get("name") or pub.get("name") or vid,
                "coords": pub.get("location"),
                "home": {u: OverviewBuilder._to_int(source.get(u))
                         for u in cls.SNIPE_UNITS},
                "home_fresh": live is not None,
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
            "default_units": cls.DEFAULT_UNITS,
            # The player's own rally-point templates, so a snipe set worked out
            # once in-game ("1000 spear", a spear/sword mix) can be poured into
            # every village at once instead of typed per village.
            "templates": DataReader.troop_templates(),
            "speeds": {u: speeds.get(u) for u in cls.SNIPE_UNITS
                       if speeds.get(u)},
            "world_speed": ws,
            "unit_speed": us,
            "default_offset_ms": cls.DEFAULT_OFFSET_MS,
            "default_min_pct": cls.DEFAULT_MIN_PCT,
            "home_age": reading_age,
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


class AccountManagerOverview:
    """What the Account manager page shows: the plan, and what the game says.

    The plan is (group -> template) rows per screen; the "what the game says"
    half is whatever the bot last read off the manager's own screens
    (cache/am_state.json). Everything here is a read - the page never talks to
    TribalWars itself, it asks the bot to, which is the process that owns the
    session and its pacing.
    """

    # section key -> (label, in-game screen, the tab's Dutch name in game)
    SECTIONS = {
        "building": ("Building", "am_village", "Bouw"),
        "troops": ("Troops", "am_troops", "Troepen"),
        "research": ("Research", "am_research", "Ontwikkeling"),
    }

    @staticmethod
    def _managed(section, village):
        """Is the manager actually doing this village?

        Read off what the screen shows rather than its wording: the building and
        research screens leave the template column empty for a village nobody
        manages, and the troop screen leaves every target blank. Both survive a
        game language the dashboard does not speak.
        """
        if section == "troops":
            return any(str(v).strip() not in ("", "0")
                       for v in (village.get("units") or {}).values())
        return bool((village.get("template") or "").strip())

    @staticmethod
    def _queued(village):
        """Build orders the manager still has queued here, from its "12 / 50"."""
        raw = (village.get("orders") or "").split("/")[0].strip()
        try:
            return int(raw)
        except ValueError:
            return None

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        flags = config.get("account_manager", {}) or {}
        state = DataReader.am_state_grab()
        plans = DataReader.am_plans_grab()
        snapshots = state.get("sections") or {}
        # Group membership comes from the incoming poller's group cache, which
        # is the only place that knows which villages are in a group - the
        # manager screens only ever show the group being looked at.
        members = {g.get("id"): g.get("villages") or []
                   for g in DataReader.groups_grab()}

        sections = []
        for key in DataReader.AM_SECTIONS:
            label, screen, dutch = cls.SECTIONS[key]
            snap = snapshots.get(key) or {}
            templates = snap.get("templates") or []
            # Before the first read the manager's own group menu is unknown, so
            # fall back to the poller's group cache - same ids, same names, plus
            # the [alle] pseudo-group the menu always starts with. That makes a
            # plan buildable on a world the bot has not opened the manager on
            # yet; the templates still have to come from the game.
            groups = snap.get("groups") or (
                [{"id": "0", "name": "alle", "type": "all"}]
                + [{"id": g.get("id"), "name": g.get("name"),
                    "type": g.get("type")}
                   for g in DataReader.groups_grab()])
            villages = snap.get("villages") or []
            def group_size(gid):
                """How many villages a group holds, or None when unknown.

                [alle] has no membership list of its own (its size is simply
                every village the screen listed), and a group the poller has not
                cached yet has an unknown one - both answer None rather than a 0
                that would read as "empty".
                """
                if gid == "0":
                    return len(villages) or None
                return len(members[gid]) if gid in members else None

            # Carried on the group itself so the page can keep the count honest
            # while a row is being edited, without a reload.
            groups = [dict(g, villages=group_size(g.get("id"))) for g in groups]
            by_template = {t.get("id"): t for t in templates}
            by_group = {g.get("id"): g for g in groups}

            rows = []
            for row in plans.get(key) or []:
                gid = row.get("group_id")
                tid = row.get("template_id")
                group = by_group.get(gid)
                template = by_template.get(tid)
                rows.append({
                    "group_id": gid,
                    "template_id": tid,
                    "group_name": (group or {}).get("name")
                                  or row.get("group_name") or gid,
                    "template_name": (template or {}).get("name")
                                     or row.get("template_name") or tid,
                    "villages": group_size(gid),
                    # Only claim something is gone once the page has actually
                    # been read; an empty snapshot means "not read yet".
                    "template_gone": bool(templates) and template is None,
                    "group_gone": bool(groups) and group is None,
                })

            managed = [v for v in villages if cls._managed(key, v)]
            idle = [v for v in managed if cls._queued(v) == 0] \
                if key == "building" else []
            # A section whose switch is off is not applied - the switches say
            # which jobs the manager is doing, and setting up one it is not
            # doing is not what anyone means. Say so where the rows are, rather
            # than letting the run quietly skip them.
            handled = True
            if flags.get("enabled"):
                handled = bool(flags.get(
                    {"building": "building", "troops": "recruiting",
                     "research": "research"}[key], False))
            sections.append({
                "key": key, "label": label, "screen": screen, "dutch": dutch,
                "handled": handled,
                "templates": templates, "groups": groups, "villages": villages,
                "rows": rows, "when": snap.get("when"),
                "total": len(villages), "managed": len(managed),
                "idle": len(idle),
            })

        result = state.get("last_result") or {}
        return {
            "flags": {
                "enabled": bool(flags.get("enabled")),
                "building": bool(flags.get("building")),
                "recruiting": bool(flags.get("recruiting")),
                "research": bool(flags.get("research")),
                "auto_setup": bool(flags.get("auto_setup")),
            },
            "sections": sections,
            "read_when": state.get("when"),
            "last_run": state.get("last_run"),
            "last_run_ts": state.get("last_run_ts"),
            "last_result": result.get("rows") or [],
            "last_result_when": result.get("when"),
            "last_result_source": result.get("source"),
            "pending_run": bool(plans.get("run_now")),
            "pending_refresh": bool(plans.get("refresh")),
            "never_read": not snapshots,
        }


class EventOverview:
    """What the Events page shows: the running event, and what the bot got out
    of past ones.

    Everything here is read off the files the bot writes; the page never talks
    to TribalWars. The energy figure is the one exception that needs arithmetic
    rather than a lookup - the game only ever reports a value and the moment it
    was true, so "now" is projected from the refill rate, the same way the bot
    does it before deciding whether it can act.
    """

    @staticmethod
    def _energy_now(snapshot):
        """Energy at this moment, projected from the bot's last look."""
        try:
            value = float(snapshot["energy"])
            top = float(snapshot["energy_max"])
            rate = float(snapshot.get("energy_rate") or 0)
            at = float(snapshot["at"])
        except (KeyError, TypeError, ValueError):
            return None, None, None
        if rate <= 0:
            return value, top, None
        grown = min(value + (time.time() - at) / rate, top)
        # Seconds until the next whole unit lands, for the countdown; None once
        # the bar is full, because a full bar has stopped refilling.
        nxt = None
        if grown < top:
            nxt = int((1 - (grown - int(grown))) * rate)
        return grown, top, nxt

    @classmethod
    def _decorate(cls, state):
        """One event file turned into what the page needs."""
        snapshot = state.get("snapshot") or {}
        totals = state.get("totals") or {}
        energy, top, nxt = cls._energy_now(snapshot)
        options = snapshot.get("options") or []
        best = options[0] if options else None

        # How the dice actually fell against what the choices were worth. Over a
        # week this is the only honest answer to "is it working" - the reward
        # alone cannot distinguish a good strategy from a lucky one.
        expected = float(totals.get("expected") or 0)
        earned = int(totals.get("reward") or 0)
        luck = (earned / expected) if expected > 0 else None

        # Cheering is not the only income. The horse race pays the whole team
        # once a day for laps completed and how the race finished - 1,950 and
        # 3,325 on the two days measured, against roughly 1,500 a day of
        # cheering. A forecast built on actions alone is therefore wrong by more
        # than half, which is exactly how it read.
        #
        # There is no endpoint for it, but the balance after every action is on
        # file, so anything the balance gained between two actions came from
        # somewhere else: a daily payout, or the player cheering by hand.
        # Gaps in the balance run both ways: a daily payout or a hand-played
        # cheer pushes it up, spending in the event shop pulls it down. Summing
        # them together produced a single "not from playing" figure that went
        # negative once the shop was used, which reads as nonsense. Kept apart.
        other, spent, payouts = 0, 0, []
        log = sorted(state.get("log") or [], key=lambda a: a.get("ts") or 0)
        previous = None
        for action in log:
            if previous and action.get("currency") is not None \
                    and previous.get("currency") is not None:
                # "reward" on the horse race, "scales" on the dragons board -
                # the same thing under the name its own driver writes.
                paid = int(action.get("reward") or action.get("scales") or 0)
                gap = (action["currency"] - previous["currency"] - paid)
                if gap > 0:
                    other += gap
                    # Big enough to be a payout rather than a few hand-clicks.
                    if gap >= 500:
                        payouts.append(gap)
                elif gap < 0:
                    spent -= gap
            previous = action
        per_day = (sum(payouts) / len(payouts)) if payouts else 0

        # What is still to come: every hour left is one more unit of energy,
        # plus whatever is already in the bar.
        # What an action is worth: the best option's rated value where the event
        # has options to choose between, otherwise what the actions actually
        # taken have averaged. The dragons board has nothing to choose - one
        # button - so its own record is the only estimate there is.
        per_action = None
        if best:
            per_action = best["value"]
        elif totals.get("actions"):
            per_action = earned / float(totals["actions"])

        forecast = None
        ends = state.get("ends_ts")
        if ends and per_action and energy is not None and not state.get("finished"):
            hours_left = max(0.0, (ends - time.time()) / 3600.0)
            actions_left = int(hours_left + energy)
            cheering = int(actions_left * per_action)
            # Nearest, not floor: the payout lands at a fixed hour each day, so
            # 47 hours left spans two of them, and flooring lost a whole one -
            # which on this event is a bigger error than everything the
            # remaining cheering is worth.
            days_left = int(round(hours_left / 24.0))
            payout = int(days_left * per_day)
            forecast = {"hours": round(hours_left, 1), "actions": actions_left,
                        "cheering": cheering, "payouts": payout,
                        "days": days_left, "reward": cheering + payout}

        return {
            "screen": state.get("screen"),
            "name": state.get("name"),
            "player_id": state.get("player_id"),
            "label": state.get("label"),
            "unsupported": bool(state.get("unsupported")),
            "finished": bool(state.get("finished")),
            "finished_at": state.get("finished_at"),
            "first_seen": state.get("first_seen"),
            "last_seen": state.get("last_seen"),
            "ends_ts": ends,
            "ends_text": state.get("ends_text"),
            "seen_at": snapshot.get("at"),
            "energy": None if energy is None else round(energy, 2),
            "energy_max": top,
            "energy_next": nxt,
            "currency": snapshot.get("currency"),
            "options": options,
            "best": best,
            "ranks_best": snapshot.get("ranks_best") or [],
            "ranks_unluckiest": snapshot.get("ranks_unluckiest") or [],
            "group": snapshot.get("group") or {},
            "currency_name": snapshot.get("currency_name") or "",
            # The dragons board: where the coin stands, what the log says, and
            # what the squares have handed over. Empty on events that have no
            # board, which is what the page keys its panels off.
            "board": snapshot.get("board") or {},
            "logs": snapshot.get("logs") or [],
            "by_square": state.get("by_square") or {},
            "items_won": state.get("items_won") or {},
            "totals": {"actions": int(totals.get("actions") or 0),
                       "jackpots": int(totals.get("jackpots") or 0),
                       "reward": earned,
                       "expected": int(expected),
                       "rolls": int(totals.get("rolls") or 0),
                       "pips": int(totals.get("pips") or 0),
                       "dragons": int(totals.get("dragons") or 0),
                       "items": int(totals.get("items") or 0)},
            "luck": None if luck is None else round(luck, 2),
            "other": other,
            "spent": spent,
            "per_day": int(per_day),
            "by_option": state.get("by_option") or {},
            "log": (state.get("log") or [])[:25],
            "forecast": forecast,
        }

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        settings = config.get("events", {}) or {}
        states = DataReader.events_grab()
        current, history = None, []
        for state in states:
            view = cls._decorate(state)
            if view["finished"] or current is not None:
                history.append(view)
            else:
                current = view
        return {
            "auto_play": bool(settings.get("auto_play", False)),
            "option": str(settings.get("option", "auto")),
            "current": current,
            "history": history,
            "ever": bool(states),
        }


class MintingOverview:
    """What the Minting page shows: the coin village, and whether it is fed.

    Coins buy noble limits, so the number that matters is not "how many
    resources arrived" but "how many coins will that mint" - the ratio is the
    whole game, since a warehouse of iron and no clay mints nothing.
    """

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        settings = config.get("minting", {}) or {}
        state = {}
        try:
            path = DataReader.data_path("cache", "minting.json")
            if os.path.exists(path):
                with open(path) as handle:
                    state = json.load(handle) or {}
        except (OSError, ValueError):
            state = {}

        village_id = str(settings.get("village") or "")
        managed = data.get("bot", {}) or {}
        village = managed.get(village_id) or {}
        academy = state.get("academy") or {}
        cost = academy.get("cost") or {}

        def coins_from(amounts):
            """A pile of resources is worth the fewest coins any one of them
            allows - the point of asking in coin ratio."""
            if not cost or not amounts:
                return None
            return min(int((amounts.get(r) or 0) / cost[r])
                       for r in ("wood", "stone", "iron") if cost.get(r))

        held = {r: int((village.get("resources") or {}).get(r) or 0)
                for r in ("wood", "stone", "iron")}

        # How many coins have appeared since the bot started watching, and since
        # midnight. The game reports a running total only, so the interesting
        # number - what arrived recently - has to be differenced out of samples.
        history = state.get("coin_history") or []
        minted = {"since": None, "coins": None, "today": None, "per_hour": None}
        if len(history) >= 2 and history[-1].get("coins") is not None:
            first, last = history[0], history[-1]
            minted["since"] = first.get("ts")
            minted["coins"] = last["coins"] - first["coins"]
            hours = max((last["ts"] - first["ts"]) / 3600.0, 1 / 60.0)
            minted["per_hour"] = round(minted["coins"] / hours, 1)
            midnight = datetime.datetime.combine(
                datetime.date.today(), datetime.time()).timestamp()
            todays = [h for h in history if h.get("ts", 0) >= midnight]
            if todays:
                # The last sample before midnight is the day's true starting
                # point; without it a day's first sample looks like zero growth.
                before = [h for h in history if h.get("ts", 0) < midnight]
                base = before[-1]["coins"] if before else todays[0]["coins"]
                minted["today"] = last["coins"] - base
        incoming = state.get("incoming") or {}
        arriving = {r: int(held.get(r, 0)) + int(incoming.get(r, 0))
                    for r in ("wood", "stone", "iron")}

        groups = [{"id": "0", "name": "alle"}] + [
            {"id": g.get("id"), "name": g.get("name")}
            for g in DataReader.groups_grab()]

        return {
            "enabled": bool(settings.get("enabled")),
            "village_id": village_id,
            "village_name": village.get("name") or village_id,
            "village_missing": bool(village_id) and not village,
            "group": str(settings.get("group", "0") or "0"),
            "groups": groups,
            "ratio": str(settings.get("ratio", "coin") or "coin"),
            "interval": int(settings.get("interval_minutes", 60) or 60),
            "keep": int(settings.get("keep", 0) or 0),
            "villages": [{"id": v, "name": (d.get("name") or v)}
                         for v, d in sorted(managed.items(),
                                            key=lambda kv: str(kv[1].get("name")))],
            "coins": academy.get("coins"),
            "cost": cost,
            "discount": academy.get("discount"),
            "auto_mint": academy.get("auto_mint"),
            "auto_status": academy.get("auto_status"),
            "read_when": academy.get("when"),
            "held": held,
            "incoming": incoming,
            "coins_now": coins_from(held),
            "coins_after": coins_from(arriving),
            "minted": minted,
            "requested_total": state.get("requested_total") or {},
            "requested_coins": coins_from(state.get("requested_total") or {}),
            "runs_with_requests": state.get("runs_with_requests") or 0,
            "last_run": state.get("last_run"),
            "next_run": (int(state.get("last_run") or 0)
                         + int(settings.get("interval_minutes", 60) or 60) * 60
                         if state.get("last_run") and settings.get("enabled") else None),
            "asked": state.get("asked_villages"),
            "candidates": state.get("candidates"),
            "last_total": state.get("last_total") or {},
            "last_coins": coins_from(state.get("last_total") or {}),
            "last_asks": state.get("last_asks") or [],
            "pending": bool(state.get("run_now")),
        }


class FlagsOverview:
    """What the Flags page shows: the plan, the pool, and who is carrying what.

    Flags are an account-wide inventory - one flag sits on exactly one village -
    so the page's job is to put demand and supply next to each other. "48
    villages want a defence flag and you own three" is the thing the old
    per-village setting could never say, and the thing that explains every
    outcome underneath it.

    Everything here is a read. The page never talks to TribalWars itself; it
    asks the bot to, which is the process that owns the session and its pacing.
    """

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        settings = config.get("flags", {}) or {}
        plan = DataReader.flag_plan_grab()
        state = DataReader.flag_state_grab()
        managed = DataReader.cache_grab("managed") or {}
        groups = DataReader.groups_grab()
        village_ids = [str(v) for v in (config.get("villages") or {})]

        def _name(village_id):
            entry = managed.get(str(village_id)) or {}
            return entry.get("name") or str(village_id)

        # The plan's own reading of itself, so the page and the bot cannot
        # disagree about which village a row covers.
        desired = flagmodule.resolve_desired(
            plan.get("rows"), groups, village_ids, config) if flagmodule else {}
        readings = state.get("readings") or {}
        inventory = flagmodule._inventory(state) if flagmodule else {}

        # Who is standing on what, so "owned" can be split into placed + spare.
        placed = {}
        for reading in readings.values():
            flag = (reading or {}).get("flag")
            if flag:
                placed[int(flag[0])] = placed.get(int(flag[0]), 0) + 1

        wanted = flagmodule.demand(desired) if flagmodule else {}
        types = []
        for type_id in (flagmodule.FLAG_TYPE_ORDER if flagmodule else ()):
            levels = inventory.get(type_id) or {}
            owned = sum(levels.values())
            name, effect = flagmodule.FLAG_TYPES[type_id]
            types.append({
                "id": type_id, "name": name, "effect": effect,
                "owned": owned,
                "levels": [{"level": lvl, "count": levels[lvl]}
                           for lvl in sorted(levels, reverse=True)],
                "best": max(levels) if levels else None,
                "placed": placed.get(type_id, 0),
                "spare": max(0, owned - placed.get(type_id, 0)),
                "want": wanted.get(type_id, 0),
                # The number that decides whether the plan can be met at all.
                "short": max(0, wanted.get(type_id, 0) - owned),
            })

        members = {str(g.get("id")): [str(v) for v in (g.get("villages") or [])]
                   for g in groups}
        rows = []
        for row in plan.get("rows") or []:
            group_id = str(row.get("group_id") or "")
            known = group_id in ("0", "all") or group_id in members
            rows.append({
                "group_id": group_id,
                "group_name": row.get("group_name") or group_id,
                "flag_type": int(row.get("flag_type") or 0),
                "flag_name": flagmodule.flag_name(row.get("flag_type"))
                             if flagmodule else str(row.get("flag_type")),
                "villages": len(village_ids) if group_id in ("0", "all")
                            else len(members.get(group_id) or []),
                # A group renamed or deleted in game stops matching rather than
                # quietly pointing somewhere else, so say so on the row.
                "missing": not known,
            })

        villages = []
        for village_id in village_ids:
            want = desired.get(village_id) or {}
            reading = readings.get(village_id) or {}
            current = reading.get("flag")
            want_type = int(want.get("flag_type") or 0)
            villages.append({
                "id": village_id,
                "name": _name(village_id),
                "want": want_type,
                "want_name": flagmodule.flag_name(want_type)
                             if (flagmodule and want_type) else None,
                "source": want.get("source"),
                "group": want.get("group"),
                "current": int(current[0]) if current else None,
                "current_name": flagmodule.flag_name(current[0])
                                if (flagmodule and current) else None,
                "level": int(current[1]) if current else None,
                "read": bool(reading),
                "ok": bool(current) and int(current[0]) == want_type,
            })
        villages.sort(key=lambda v: (v["ok"], str(v["name"]).lower()))

        unmet = []
        for entry in state.get("unmet") or []:
            unmet.append({
                "id": entry.get("village_id"),
                "name": _name(entry.get("village_id")),
                "flag_type": entry.get("flag_type"),
                "flag_name": flagmodule.flag_name(entry.get("flag_type"))
                             if flagmodule else None,
            })

        results = []
        for entry in state.get("last_result") or []:
            results.append(dict(entry, name=_name(entry.get("village_id")),
                                flag_name=flagmodule.flag_name(entry.get("flag_type"))
                                if flagmodule else None))

        return {
            "available": flagmodule is not None,
            "manage": bool(settings.get("manage")),
            "auto_assign": bool(settings.get("auto_assign")),
            "auto_upgrade": bool(settings.get("auto_upgrade")),
            "world_has_flags": (config.get("world") or {}).get("flags_enabled"),
            "rows": rows,
            "groups": [{"id": "0", "name": "All villages", "type": "all",
                        "villages": len(village_ids)}] +
                      [{"id": str(g.get("id")), "name": g.get("name"),
                        "type": g.get("type"),
                        "villages": len(g.get("villages") or [])} for g in groups],
            "types": types,
            "villages": villages,
            "unmet": unmet,
            "placed_total": sum(placed.values()),
            "owned_total": sum(t["owned"] for t in types),
            "raw": state.get("raw"),
            "read_when": state.get("read_when"),
            "cooldown": bool(state.get("cooldown")),
            "unread": [{"id": v, "name": _name(v)}
                       for v in (state.get("unread") or [])],
            "last_result": results,
            "last_result_when": state.get("last_result_when"),
            "last_result_source": state.get("last_result_source"),
            "last_run": state.get("last_run"),
            "last_run_ts": state.get("last_run_ts"),
            "upgraded": state.get("upgraded") or 0,
            "pending_run": bool(plan.get("run_now")),
            "pending_refresh": bool(plan.get("refresh")),
        }


class BalancerOverview:
    """What the Resource balancing page shows: who fed whom, and what is still
    on the road.

    The balancer's whole difficulty is that a delivery already travelling is
    invisible to the receiver's own snapshot, so it writes every send down
    together with the moment it lands (cache/balancer.json). That file is what
    this reads: convoys in the air, the senders' cooldowns, and the measured
    merchant speed the arrival times are computed from.
    """

    @staticmethod
    def _total(amounts):
        return sum(int(v or 0) for v in (amounts or {}).values())

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        settings = config.get("balancer", {}) or {}
        state = DataReader.balancer_state_grab()
        managed = DataReader.cache_grab("managed") or {}
        now = time.time()

        def _name(village_id):
            entry = managed.get(str(village_id)) or {}
            return entry.get("name") or str(village_id)

        cooldown = int(settings.get("send_cooldown_minutes", 60) or 60) * 60

        # Per-sender rows. The bookkeeping keys a village id straight onto the
        # top level, so the underscore-prefixed keys are the module's own.
        senders = []
        for key, entry in state.items():
            if key.startswith("_") or not isinstance(entry, dict):
                continue
            if "last_send" not in entry:
                continue
            last = int(entry.get("last_send") or 0)
            senders.append({
                "id": key, "name": _name(key),
                "last_send": last,
                "target_id": entry.get("last_target"),
                "target_name": _name(entry.get("last_target")),
                "amounts": entry.get("last_amounts") or {},
                "total": cls._total(entry.get("last_amounts")),
                # Negative means the cooldown has already run out, which is the
                # normal state - it is only interesting while it is positive.
                "ready_in": max(0, int(last + cooldown - now)),
            })
        senders.sort(key=lambda s: -s["last_send"])

        # Convoys that have not landed yet, and what each receiver has had.
        inflight, received = [], {}
        day_ago = now - 86400
        for village_id, flights in (state.get("_inflight") or {}).items():
            for flight in flights or []:
                arrival = int(flight.get("arrival") or 0)
                if arrival > now:
                    inflight.append({
                        "to": str(village_id), "to_name": _name(village_id),
                        "sent": int(flight.get("sent") or 0),
                        "arrival": arrival,
                        "amounts": flight.get("amounts") or {},
                        "total": cls._total(flight.get("amounts")),
                    })
        inflight.sort(key=lambda f: f["arrival"])

        for village_id, stamps in (state.get("_deliveries") or {}).items():
            stamps = [int(s) for s in stamps or []]
            if not stamps:
                continue
            received[str(village_id)] = {
                "id": str(village_id), "name": _name(village_id),
                "total": len(stamps),
                "day": len([s for s in stamps if s >= day_ago]),
                "last": max(stamps),
            }
        receivers = sorted(received.values(), key=lambda r: -r["last"])

        merchant = state.get("_merchant") or {}
        no_market = [{"id": str(v), "name": _name(v), "when": int(t or 0)}
                     for v, t in (state.get("_no_market") or {}).items()]
        no_market.sort(key=lambda m: -m["when"])

        return {
            "enabled": bool(settings.get("enabled")),
            "settings": {
                "sender_min_points": settings.get("sender_min_points"),
                "receiver_max_points": settings.get("receiver_max_points"),
                "target_fill_pct": settings.get("target_fill_pct"),
                "fill_mode": settings.get("fill_mode"),
                "sender_order": settings.get("sender_order"),
                "target_order": settings.get("target_order"),
                "send_cooldown_minutes": settings.get("send_cooldown_minutes"),
                "max_sends_per_receiver": settings.get("max_sends_per_receiver"),
                "reserve_merchants": settings.get("reserve_merchants"),
                "sender_keep": settings.get("sender_keep"),
                "min_send_amount": settings.get("min_send_amount"),
            },
            "senders": senders,
            "inflight": inflight,
            "inflight_total": sum(f["total"] for f in inflight),
            "receivers": receivers,
            "delivered_day": sum(r["day"] for r in receivers),
            "delivered_total": sum(r["total"] for r in receivers),
            "merchant": {
                "minutes_per_field": merchant.get("minutes_per_field"),
                "measured": merchant.get("measured"),
                "over_fields": merchant.get("over_fields"),
                "seconds": merchant.get("seconds"),
            },
            "no_market": no_market,
            "last_turn": max([int(t or 0) for t in
                              (state.get("_turns") or {}).values()] or [0]),
        }


class ReportAnalysisOverview:
    """What the Report analysis page shows: the switches, and what has already
    been written onto the map.

    The reading itself (which villages have a dead clear) is done on demand by
    /app/dead-clears, because it is a scan of every report on disk and there is
    no reason to pay for it on a page load nobody asked it of. This is the part
    that is cheap: the settings the panel should start from, and the notes the
    unattended pass has written.
    """

    @classmethod
    def build(cls, data):
        config = data.get("config", {}) or {}
        settings = config.get("report_analysis", {}) or {}
        state = DataReader.report_analysis_state_grab()
        noted = state.get("noted") or {}
        managed = DataReader.cache_grab("managed") or {}

        rows = []
        for village_id, line in noted.items():
            entry = managed.get(str(village_id)) or {}
            rows.append({"id": str(village_id),
                         "name": entry.get("name") or str(village_id),
                         "line": line})
        rows.sort(key=lambda r: str(r["line"]), reverse=True)

        defaults = {}
        if reportanalysis is not None:
            defaults = {
                "min_units": reportanalysis.DEFAULT_MIN_UNITS,
                "min_loss_pct": reportanalysis.DEFAULT_MIN_LOSS_PCT,
                "alive_max_loss_pct": reportanalysis.DEFAULT_ALIVE_MAX_LOSS_PCT,
                "note_prefix": reportanalysis.DEFAULT_NOTE_PREFIX,
                "note_prefix_alive": reportanalysis.DEFAULT_NOTE_PREFIX_ALIVE,
                "note_rebuild": reportanalysis.DEFAULT_NOTE_REBUILD,
                "rebuild_days": reportanalysis.DEFAULT_REBUILD_DAYS,
            }
        # Where the scout button points: the rally point of a village of ours
        # with the target filled in, so checking whether a nuke is back is one
        # click and one send.
        session = DataReader.get_session() or {}
        endpoint = session.get("endpoint") or ""
        game_base = (endpoint.rsplit("/", 1)[0]
                     if endpoint.startswith("http") else "")

        return {
            "available": reportanalysis is not None,
            "job": state.get("job") or {},
            "min_units": settings.get("min_units", defaults.get("min_units")),
            "min_loss_pct": settings.get("min_loss_pct",
                                         defaults.get("min_loss_pct")),
            "alive_max_loss_pct": settings.get(
                "alive_max_loss_pct", defaults.get("alive_max_loss_pct")),
            "note_prefix": settings.get("note_prefix",
                                        defaults.get("note_prefix")),
            "note_prefix_alive": settings.get(
                "note_prefix_alive", defaults.get("note_prefix_alive")),
            "note_rebuild": settings.get("note_rebuild",
                                         defaults.get("note_rebuild")),
            "rebuild_days": settings.get("rebuild_days",
                                         defaults.get("rebuild_days")),
            "game_base": game_base,
            "home": next(iter(managed), ""),
            "noted": rows,
            "noted_count": len(rows),
            "last_run": state.get("last_run"),
            "reports": len(DataReader.cache_grab("reports") or {}),
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
    def _endpoint_for(world):
        """The game endpoint configured for a world, or None if not set up."""
        key = BotManager._world_key(world)
        base = (os.path.join(DataReader.project_root(), "worlds", key)
                if key else DataReader.project_root())
        try:
            with open(os.path.join(base, "config.json"), encoding="utf-8") as fh:
                return json.load(fh).get("server", {}).get("endpoint")
        except (OSError, ValueError, AttributeError):
            return None

    @staticmethod
    def find_bot_pid(world=None):
        """Locate the running twb.py process for a given world.

        Matches a python process running twb.py whose --world matches (default
        world = no --world). Scanning by command line keeps the status accurate
        even when the bot was started by hand rather than through this UI.

        Falls back to the account lock (core/instance_lock.py), which catches a
        bot playing this same account under a *different* --world name - most
        commonly start.bat launching the default world while the dashboard has
        the same account selected as worlds/<name>. Without that fallback the
        status reads "stopped" and "Start bot" launches a second instance, which
        logs the first one out.
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
        endpoint = BotManager._endpoint_for(world)
        if endpoint:
            holder = InstanceLock.holder(endpoint)
            if holder:
                return holder.get("pid")
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
