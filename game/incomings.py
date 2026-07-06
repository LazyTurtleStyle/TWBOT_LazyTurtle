"""
Incoming-attack tracking and tagging.

The bot's defence manager only knows *whether* a village is under attack (a
boolean derived from the account-wide incoming counter). This module fills in
the detail: it scrapes the incomings overview, records each individual command
(origin village/player, arrival time, the moment we first saw it) and keeps that
in cache/incomings so the web dashboard can show per-attack information.

It also exposes pure helpers (distance, per-unit travel time and an auto-tag
suggestion) that the web manager reuses to render walking times without having
to talk to the game itself.

"Tagging" is the TribalWars practice of identifying an incoming by its slowest
possible unit: a flight slower than a ram must contain a noble, a very fast
flight is probably a fake. The game tags by picking "the slowest detected unit,
based on the remaining travel time and the assumption that the command was just
sent"; slowest_floor() is our estimator for that.
"""

import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from core.extractors import Extractor
from core.filemanager import FileManager
from core.notification import Notification

# Standard TribalWars unit speeds (minutes per field at world speed 1 /
# unit_speed 1). Only used as a fallback when the world's real values have not
# been cached yet; the live values from interface.php take precedence.
DEFAULT_UNIT_SPEEDS = {
    "spear": 18, "sword": 22, "axe": 18, "archer": 18, "spy": 9,
    "light": 10, "marcher": 10, "heavy": 11, "ram": 30, "catapult": 30,
    "knight": 10, "snob": 35,
}

# Display order for the walking-time table (slowest-relevant units last-ish);
# militia is excluded as it never travels.
UNIT_ORDER = [
    "spy", "light", "marcher", "knight", "heavy", "axe", "archer", "spear",
    "sword", "ram", "catapult", "snob",
]

WORLD_CONFIG_CACHE = "cache/world/config.json"
WORLD_UNITS_CACHE = "cache/world/unit_info.json"
# The player's in-game village groups (manual + dynamic) with their current
# member villages; the snipe tab filters its source villages by these.
GROUPS_CACHE = "cache/world/groups.json"
# When the group menu response cannot be parsed, the raw payload is dumped
# here so the parser can be adapted to what the server actually sends.
GROUPS_RAW_DUMP = "cache/world/groups_raw.json"
INCOMINGS_DIR = "cache/incomings"
# The in-game "rename incoming attack" request, captured from the live incomings
# page so we replicate exactly what the browser does instead of guessing.
LABEL_ENDPOINT_CACHE = "cache/world/incoming_label.json"
# Tracks whether the last poll saw a logged-out page, so a dead cookie warns
# once on transition instead of every single poll cycle. Kept out of
# INCOMINGS_DIR so it is not mistaken for a command by _prune or the dashboard.
SESSION_STATE_CACHE = "cache/world/incoming_session.json"

# How long a cached copy of the world speed data is considered fresh.
WORLD_CACHE_TTL = 24 * 3600
# Group membership changes (dynamic groups especially), so refresh hourly.
GROUPS_CACHE_TTL = 3600
# While the session stays logged out, re-send the warning at most this often.
SESSION_RENOTIFY_TTL = 6 * 3600


def field_distance(a, b):
    """Euclidean field distance between two (x, y) coordinate pairs."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit_travel_seconds(distance, base_speed, world_speed, unit_speed):
    """Full travel time (seconds) of a unit across `distance` fields.

    base_speed is the unit's minutes-per-field at speed 1; world_speed and
    unit_speed are the world's config multipliers. Both make troops *faster*
    (shorter travel time), so both divide the base time:

        seconds = base_speed * distance / (world_speed * unit_speed) * 60
    """
    if not base_speed or world_speed <= 0 or unit_speed <= 0:
        return 0.0
    return distance * base_speed / (world_speed * unit_speed) * 60.0


def travel_table(distance, unit_speeds, world_speed, unit_speed):
    """Map of unit -> full travel seconds for the given distance."""
    return {
        unit: unit_travel_seconds(distance, base, world_speed, unit_speed)
        for unit, base in unit_speeds.items()
    }


def slowest_floor(table, remaining_seconds):
    """The *fastest* unit the attack could still be - the real tagging estimate.

    We never learn the enemy's send time (the game does not reveal incoming unit
    composition to the defender). We only know the arrival time and the moment we
    first saw the command. So the only hard fact is:

        true flight time F = arrival - sent
        we saw it at first_seen >= sent, so F >= arrival - first_seen = remaining

    F equals the travel time of the *slowest* unit in the attack, so that slowest
    unit's travel time is at least `remaining`. The tightest thing we can say is
    therefore the *fastest* unit whose travel time is still >= remaining: the
    attack is at least that slow. The slower we were to detect it, the smaller
    `remaining` is and the less this narrows things down - which is exactly why
    catching attacks early (the background poller) matters.

    Returns the unit name, or None when remaining exceeds even the slowest unit's
    travel time (detection timing is inconsistent / stale).
    """
    candidates = [
        (unit, secs) for unit, secs in table.items()
        if secs > 0 and secs >= remaining_seconds
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[1])[0]


def load_world_speeds():
    """Return (world_speed, unit_speed, {unit: base_speed}) from cache.

    Falls back to standard TribalWars values when the world data has not been
    fetched yet, so travel times are always available (if approximate).
    """
    config = FileManager.load_json_file(WORLD_CONFIG_CACHE) or {}
    units = FileManager.load_json_file(WORLD_UNITS_CACHE) or {}
    world_speed = float(config.get("speed", 1) or 1)
    unit_speed = float(config.get("unit_speed", 1) or 1)
    speeds = units.get("speeds") if isinstance(units, dict) else None
    if not speeds:
        speeds = dict(DEFAULT_UNIT_SPEEDS)
    return world_speed, unit_speed, speeds


def parse_coords(text):
    """Extract the first (x|y) coordinate pair from a string, or None."""
    match = re.search(r"\((\d+)\|(\d+)\)", text or "")
    if match:
        return [int(match.group(1)), int(match.group(2))]
    return None


def _parse_countdown(text):
    """Turn a 'H:MM:SS' or 'MM:SS' timer string into seconds, or None."""
    parts = [p for p in re.findall(r"\d+", text or "")]
    if not parts:
        return None
    parts = [int(p) for p in parts]
    seconds = 0
    for value in parts:
        seconds = seconds * 60 + value
    return seconds


def _parse_arrival_clock(text):
    """Extract (hour, minute, second, millis|None) from an arrival cell.

    The incomings overview renders arrivals as e.g. 'vandaag om 23:20:43:387'
    (the trailing :387 being milliseconds when the world shows them) or with a
    date prefix like 'op 05.07. om 23:20:43'. Only the H:MM:SS(:mmm) clock is
    matched; the dotted date can never satisfy the colon-separated pattern.
    """
    match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})(?::(\d{1,3}))?", text or "")
    if not match:
        return None
    return (
        int(match.group(1)), int(match.group(2)), int(match.group(3)),
        int(match.group(4)) if match.group(4) is not None else None,
    )


class IncomingManager:
    """Scrapes incoming attacks and keeps cache/incomings up to date."""

    def __init__(self, village_id=None, wrapper=None):
        self.village_id = village_id
        self.wrapper = wrapper
        self.logger = logging.getLogger("Incomings")

    def run(self):
        """Refresh world speeds (if needed) and the incoming-command cache."""
        FileManager.create_directories([INCOMINGS_DIR, "cache/world"])
        self.ensure_world_data()
        self.ensure_groups()
        return self.update_incomings()

    # -- world speed data ---------------------------------------------------

    def ensure_world_data(self):
        """Fetch + cache the world's speed config and unit speeds once a day."""
        config = FileManager.load_json_file(WORLD_CONFIG_CACHE)
        if config and (time.time() - config.get("_fetched", 0)) < WORLD_CACHE_TTL:
            return
        try:
            cfg_res = self.wrapper.get_url("interface.php?func=get_config")
            unit_res = self.wrapper.get_url("interface.php?func=get_unit_info")
        except Exception as exc:  # network hiccup: keep any existing cache
            self.logger.warning("Could not fetch world data: %s", exc)
            return
        self._store_world_config(cfg_res)
        self._store_unit_info(unit_res)

    def _store_world_config(self, response):
        if not response:
            return
        try:
            root = ET.fromstring(response.text)
            data = {
                "speed": float(root.findtext("speed") or 1),
                "unit_speed": float(root.findtext("unit_speed") or 1),
                # Cancel-snipe needs these: how long after sending a command may
                # still be cancelled, and whether the world displays milliseconds
                # on arrival times (all modern worlds do).
                "command_cancel_time": int(float(
                    root.findtext("commands/command_cancel_time") or 600)),
                "millis_arrival": int(float(
                    root.findtext("commands/millis_arrival") or 0)),
                "_fetched": int(time.time()),
            }
            FileManager.save_json_file(data, WORLD_CONFIG_CACHE)
            self.logger.info(
                "Cached world speed: speed=%s unit_speed=%s",
                data["speed"], data["unit_speed"]
            )
        except ET.ParseError as exc:
            self.logger.warning("Could not parse world config: %s", exc)

    def _store_unit_info(self, response):
        if not response:
            return
        try:
            root = ET.fromstring(response.text)
            speeds = {}
            carry = {}
            for unit in root:
                speed = unit.findtext("speed")
                if speed is not None:
                    speeds[unit.tag] = float(speed)
                haul = unit.findtext("carry")
                if haul is not None:
                    carry[unit.tag] = float(haul)
            if speeds:
                FileManager.save_json_file(
                    {"speeds": speeds, "carry": carry,
                     "_fetched": int(time.time())},
                    WORLD_UNITS_CACHE,
                )
        except ET.ParseError as exc:
            self.logger.warning("Could not parse unit info: %s", exc)

    # -- village groups -------------------------------------------------------

    def ensure_groups(self):
        """Fetch + cache the in-game village groups (manual and dynamic) with
        their current member villages, at most once per GROUPS_CACHE_TTL.

        The group list comes from the group menu's ajax endpoint; membership
        from the villages overview filtered on each group - both GETs the
        browser itself makes, so no new endpoint shapes are guessed."""
        cached = FileManager.load_json_file(GROUPS_CACHE)
        if cached and (time.time() - cached.get("_fetched", 0)) < GROUPS_CACHE_TTL:
            return
        try:
            res = self.wrapper.get_url(
                f"game.php?village={self.village_id}&screen=groups"
                "&ajax=load_group_menu")
            if not res:
                return  # network hiccup: keep any older cache, retry next TTL
            groups = self._parse_group_menu(res.text)
            if groups is None:
                return  # unparseable: raw payload dumped, keep any older cache
            for group in groups:
                group["villages"] = self._group_villages(group["id"])
            FileManager.save_json_file(
                {"groups": groups, "_fetched": int(time.time())}, GROUPS_CACHE)
            self.logger.info("Cached %d village group(s)", len(groups))
        except Exception as exc:
            self.logger.warning("Could not fetch village groups: %s", exc)

    def _parse_group_menu(self, text):
        """The group-menu ajax payload -> [{id, name, type}], skipping the
        built-in 'all villages' pseudo group. Returns None (and dumps the raw
        payload) when the shape is not recognised."""
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            payload = None
        raw = None
        if isinstance(payload, dict):
            for key in ("result", "groups"):
                for container in (payload, payload.get("response") or {}):
                    if isinstance(container, dict) \
                            and isinstance(container.get(key), list):
                        raw = container[key]
                        break
                if raw is not None:
                    break
        if raw is None:
            try:
                FileManager.save_json_file(
                    {"raw": (text or "")[:4000]}, GROUPS_RAW_DUMP)
            except Exception:
                pass
            self.logger.warning(
                "Unrecognised group menu payload - raw copy in %s",
                GROUPS_RAW_DUMP)
            return None
        groups = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            gid = str(entry.get("group_id", "") or "")
            gtype = entry.get("type")
            if not gid or gid == "0" or gtype not in ("static", "dynamic"):
                continue
            groups.append({
                "id": gid,
                "name": str(entry.get("name") or gid),
                "type": gtype,
            })
        return groups

    def _group_villages(self, group_id):
        """Village ids currently in a group, from the villages overview page
        filtered on that group (page=-1 lists everything on one page)."""
        res = self.wrapper.get_url(
            f"game.php?village={self.village_id}&screen=overview_villages"
            f"&mode=combined&group={group_id}&page=-1")
        if not res:
            return []
        # Row village names carry quickedit spans whose data-id is the village
        # id; fall back to the row links when the markup changes.
        ids = re.findall(
            r'class="quickedit-vn[^"]*"[^>]*data-id="(\d+)"', res.text)
        if not ids:
            ids = re.findall(
                r'village=(\d+)&(?:amp;)?screen=overview(?![_a-z])', res.text)
        return sorted(set(ids), key=int)

    # -- incoming commands --------------------------------------------------

    def update_incomings(self):
        """Scrape the incomings overview and merge it into the cache."""
        url = (
            f"game.php?village={self.village_id}"
            "&screen=overview_villages&mode=incomings&subtype=attacks&group=0"
        )
        res = self.wrapper.get_url(url)
        if not res:
            return []

        status = self._session_status(res)
        if status != "ok":
            # A captcha interstitial is transient; only a genuine logout means
            # we are silently missing attacks and should warn.
            if status == "logged_out":
                self._note_logged_out()
            return []
        self._note_logged_in()

        now = self._server_time(res)
        self._capture_label_endpoint(res, now)
        commands = self.parse_incomings(res.text, now)
        seen = set()
        for command in commands:
            self._store_command(command, now)
            seen.add(str(command["command_id"]))
        self._prune(seen, now)
        self.logger.info("Tracking %d incoming attack(s)", len(seen))
        return commands

    # -- session detection --------------------------------------------------

    @staticmethod
    def _session_status(res):
        """Classify a scrape as 'ok', 'logged_out' or 'captcha'.

        Every in-game page carries a ``TribalWars.updateGameData(...)`` blob; the
        login/redirect page we get once the session cookie has expired does not.
        That presence check is what distinguishes "logged out" from a logged-in
        page that simply lists zero incomings (which we must not treat as an
        error). A forced bot-protection page is a separate, transient case.
        """
        text = getattr(res, "text", "") or ""
        if 'data-bot-protect="forced"' in text:
            return "captcha"
        return "ok" if Extractor.game_state(res) else "logged_out"

    def _note_logged_out(self):
        """Warn (log + notification) that the poller can no longer read attacks.

        Throttled via SESSION_STATE_CACHE so a dead cookie nags once when the
        session first drops and then only every SESSION_RENOTIFY_TTL, instead of
        on every poll cycle.
        """
        now = int(time.time())
        state = FileManager.load_json_file(SESSION_STATE_CACHE) or {}
        self.logger.warning(
            "Incoming poll hit a logged-out page: the session cookie has likely "
            "expired and incoming attacks are NOT being tracked. Refresh it."
        )
        last_notified = state.get("notified_at", 0)
        if not state.get("logged_out") or now - last_notified >= SESSION_RENOTIFY_TTL:
            Notification.send(
                "TWB: incoming-attack tracking is logged out - the session "
                "cookie has expired. Refresh the cookie; attacks are NOT being "
                "detected until you do."
            )
            last_notified = now
        FileManager.save_json_file(
            {"logged_out": True, "notified_at": last_notified},
            SESSION_STATE_CACHE,
        )

    def _note_logged_in(self):
        """Clear the logged-out flag (and confirm) once the session works again."""
        state = FileManager.load_json_file(SESSION_STATE_CACHE)
        if state and state.get("logged_out"):
            self.logger.info("Incoming poll session restored")
            Notification.send("TWB: incoming-attack tracking session restored.")
            FileManager.save_json_file({"logged_out": False}, SESSION_STATE_CACHE)

    # -- in-game label endpoint capture -------------------------------------

    def _capture_label_endpoint(self, res, now):
        """Record the exact request the game uses to rename a command label.

        The incomings page wires its inline label editor with a
        ``TribalWars.buildURL('POST', '<screen>', { ... })`` call. We parse that
        (plus the CSRF token from the page's game data) and cache it, so the
        dashboard can replay it to actually rename an attack in TribalWars
        instead of us guessing an endpoint. If nothing matches, we leave any
        previous capture untouched and in-game renaming simply stays inactive.
        """
        try:
            html = res.text
            candidates = []
            for match in re.finditer(
                r"buildURL\(\s*'(?:GET|POST)'\s*,\s*'([^']+)'\s*,\s*\{([^}]*)\}",
                html,
            ):
                params = dict(re.findall(r"(\w+)\s*:\s*'([^']*)'", match.group(2)))
                candidates.append({"screen": match.group(1), "params": params})
            if not candidates:
                return

            def score(cand):
                blob = (
                    cand["screen"] + " "
                    + " ".join(cand["params"].keys()) + " "
                    + " ".join(cand["params"].values())
                ).lower()
                value = 0
                for keyword in ("command", "rename", "label", "subject"):
                    if keyword in blob:
                        value += 2
                if cand["params"].get("mode") == "incomings":
                    value += 2
                if cand["screen"] == "overview_villages":
                    value += 1
                if "change_name" in blob:  # that is the village-name editor
                    value -= 5
                return value

            best = max(candidates, key=score)
            if score(best) <= 0:
                return  # nothing that looks like a command-label editor

            game_data = Extractor.game_state(res) or {}
            csrf = game_data.get("csrf")
            FileManager.save_json_file(
                {
                    "screen": best["screen"],
                    "params": best["params"],
                    "csrf": csrf,
                    "_fetched": now,
                    "_candidates": candidates,  # kept for debugging / verification
                },
                LABEL_ENDPOINT_CACHE,
            )
            self.logger.info(
                "Captured in-game label endpoint: screen=%s params=%s",
                best["screen"], best["params"],
            )
        except Exception as exc:  # never let capture break the scrape
            self.logger.debug("Label endpoint capture failed: %s", exc)

    @staticmethod
    def _server_time(res):
        """Server time (epoch seconds) from the page, falling back to local."""
        game_data = Extractor.game_state(res)
        if game_data:
            generated = game_data.get("time_generated")
            if generated:
                return int(int(generated) / 1000)
        return int(time.time())

    def parse_incomings(self, html, now):
        """Parse the incomings_table into a list of command dicts."""
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="incomings_table")
        if not table:
            return []
        commands = []
        for row in table.find_all("tr"):
            command = self._parse_row(row, now)
            if command:
                commands.append(command)
        return commands

    def _parse_row(self, row, now):
        cells = row.find_all("td")
        if len(cells) < 7:
            return None  # header / footer row

        quickedit = cells[0].find(attrs={"data-id": True})
        if not quickedit:
            return None
        command_id = quickedit["data-id"]

        label_el = cells[0].find(class_="quickedit-label")
        game_label = label_el.get_text(strip=True) if label_el else None

        target_link = cells[1].find("a")
        target_coords = parse_coords(target_link.get_text() if target_link else "")
        target_id = self.village_id
        if target_link:
            tid = re.search(r"village=(\d+)", target_link.get("href", ""))
            if tid:
                target_id = tid.group(1)

        origin_link = cells[2].find("a")
        origin_coords = parse_coords(origin_link.get_text() if origin_link else "")
        origin_name = origin_link.get_text(strip=True) if origin_link else None
        origin_id = None
        if origin_link:
            oid = re.search(r"id=(\d+)", origin_link.get("href", ""))
            if oid:
                origin_id = oid.group(1)
            if origin_name:
                origin_name = re.sub(r"\s*\(\d+\|\d+\).*$", "", origin_name).strip()

        player_link = cells[3].find("a")
        player_name = player_link.get_text(strip=True) if player_link else None
        player_id = None
        if player_link:
            pid = re.search(r"id=(\d+)", player_link.get("href", ""))
            if pid:
                player_id = pid.group(1)

        countdown = _parse_countdown(cells[6].get_text())
        arrival = now + countdown if countdown is not None else None

        # Millisecond-precise arrival, needed for cancel-sniping. The countdown
        # pins the arrival to within a couple of seconds; the wall clock in the
        # arrival column then pins the exact second via its second-of-minute
        # (timezone-proof, so we never need to know the server's TZ) and
        # contributes the millisecond part.
        arrival_ms = None
        clock = _parse_arrival_clock(cells[5].get_text())
        if arrival is not None and clock:
            _h, _m, second, millis = clock
            arrival += (second - arrival % 60 + 30) % 60 - 30
            if millis is not None:
                arrival_ms = arrival * 1000 + millis

        distance = None
        if origin_coords and target_coords:
            distance = round(field_distance(origin_coords, target_coords), 2)

        return {
            "command_id": command_id,
            "target_id": str(target_id) if target_id else None,
            "target_coords": target_coords,
            "origin_id": origin_id,
            "origin_coords": origin_coords,
            "origin_name": origin_name,
            "player_id": player_id,
            "player_name": player_name,
            "distance": distance,
            "arrival": arrival,
            "arrival_ms": arrival_ms,
            "game_label": game_label,
        }

    def _store_command(self, command, now):
        path = f"{INCOMINGS_DIR}/{command['command_id']}.json"
        existing = FileManager.load_json_file(path) or {}
        # Preserve the original detection moment and any user-set tag. A poll
        # that failed to read the millisecond part must not wipe one we already
        # captured (the c-snipe planner depends on it).
        command["first_seen"] = existing.get("first_seen", now)
        command["tag"] = existing.get("tag")
        if command.get("arrival_ms") is None and existing.get("arrival_ms"):
            command["arrival_ms"] = existing["arrival_ms"]
        command["last_seen"] = now
        FileManager.save_json_file(command, path)

    def _prune(self, seen, now):
        """Drop commands no longer present whose arrival is already in the past."""
        try:
            files = FileManager.list_directory(INCOMINGS_DIR, ends_with=".json")
        except FileNotFoundError:
            return
        for name in files:
            command_id = name[:-len(".json")]
            if command_id in seen:
                continue
            entry = FileManager.load_json_file(f"{INCOMINGS_DIR}/{name}") or {}
            arrival = entry.get("arrival")
            if arrival is None or arrival <= now:
                FileManager.remove_file(f"{INCOMINGS_DIR}/{name}")


def load_groups():
    """Cached in-game village groups: [{id, name, type, villages}]."""
    data = FileManager.load_json_file(GROUPS_CACHE) or {}
    groups = data.get("groups")
    return groups if isinstance(groups, list) else []


def load_label_endpoint():
    """The captured in-game rename request, or None if not seen yet."""
    return FileManager.load_json_file(LABEL_ENDPOINT_CACHE)


def incoming_session_state():
    """Last known poller session state, for the dashboard.

    Returns {"logged_out": bool, "notified_at": ts} or None when the poller has
    never recorded a state yet (treated as healthy/unknown). Lets the web UI
    distinguish "session expired - tracking is blind" from "no incomings".
    """
    return FileManager.load_json_file(SESSION_STATE_CACHE)


def rename_command_ingame(command_id, label, cookies, endpoint, user_agent=None):
    """Replay the game's own label-rename request to tag an incoming attack.

    Uses the endpoint captured from the live incomings page (load_label_endpoint)
    plus the bot's session cookies. Returns a small status dict; never raises.

    No endpoint is ever guessed: if the bot has not yet scraped a logged-in
    incomings page we report 'no_endpoint_yet' and the caller keeps the tag
    local. command_id substitutes any '__ID__' placeholder the editor uses, and
    the new text is sent under the field names QuickEdit/TribalWars accept (extra
    unused keys are harmless).
    """
    cfg = load_label_endpoint()
    if not cfg or not cfg.get("screen"):
        return {"ok": False, "reason": "no_endpoint_yet"}
    if not cookies:
        return {"ok": False, "reason": "no_session"}

    base = endpoint.rsplit("/", 1)[0] if endpoint else ""
    query = ["screen=%s" % cfg["screen"]]
    for key, value in (cfg.get("params") or {}).items():
        if isinstance(value, str):
            value = value.replace("__ID__", str(command_id))
        query.append("%s=%s" % (key, value))
    if cfg.get("csrf"):
        query.append("h=%s" % cfg["csrf"])
    url = "%s/game.php?%s" % (base, "&".join(query))

    session = requests.Session()
    for key, value in cookies.items():
        session.cookies.set(key, value)
    headers = {
        "TribalWars-Ajax": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    body = {
        "id": str(command_id),
        "command_id": str(command_id),
        "value": label,
        "name": label,
        "text": label,
    }
    if cfg.get("csrf"):
        body["h"] = cfg["csrf"]
    try:
        res = session.post(url, data=body, headers=headers, timeout=(10, 30))
    except requests.RequestException as exc:
        return {"ok": False, "reason": "request_failed", "error": str(exc)}

    ok = res.status_code == 200
    error = None
    try:
        payload = res.json()
        if isinstance(payload, dict) and payload.get("error"):
            ok = False
            error = payload.get("error")
    except ValueError:
        # A non-JSON 200 usually means we were bounced to a login/HTML page.
        if "login" in res.text[:2000].lower() or "<html" in res.text[:200].lower():
            ok = False
            error = "session_invalid"
    return {"ok": ok, "status": res.status_code, "url": url, "error": error}
