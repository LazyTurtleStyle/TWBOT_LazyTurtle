"""
Class for using one generic cookie jar, emulating a single tab
"""

import json
import os
import requests

from core.filemanager import FileManager
from core.notification import Notification

import logging
import re
import time
import random
from urllib.parse import urljoin, urlencode

from core.reporter import ReporterObject


class WebWrapper:
    """
    WebWrapper object for sending HTTP requests
    """
    web = None
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/78.0.3904.97 Safari/537.36',
        'upgrade-insecure-requests': '1'
    }
    endpoint = None
    logger = logging.getLogger("Requests")
    server = None
    last_response = None
    last_h = None
    priority_mode = False
    auth_endpoint = None
    reporter = None
    delay = 1.0
    # When False, get_url returns the bot-protection page instead of waiting for
    # a manual captcha solve. Used by background pollers that have no interactive
    # console.
    block_on_captcha = True
    # While bot-protection ("forced" captcha) is active, the main loop re-checks
    # the page on this cadence until the captcha is solved (in a browser on the
    # same session) and auto-resumes - no console keypress or restart needed.
    CAPTCHA_POLL_SECONDS = 20
    # ...except while the account is meant to look asleep, where 20s is both
    # wasteful and self-defeating. A captcha at 22:40 on 2026-08-27 blocked the
    # main loop until 06:59 and cost ~1500 re-checks, most of them fired at a
    # dead session in the small hours: the noisiest stretch of a night whose
    # whole point was to be quiet. Nothing is lost by slowing down, because a
    # clear detected outside active hours only sends the main loop straight into
    # sleep_through_inactive_hours anyway - the solve is not acted on until the
    # window reopens either way. Set to 0 to disable the backoff.
    CAPTCHA_POLL_SECONDS_QUIET = 300
    # Optional callable injected by the bot loop (TWB.in_quiet_hours): returns
    # True while the account is meant to look asleep. Left None on background
    # pollers and any other wrapper with no notion of an activity window, which
    # simply keeps the normal cadence.
    quiet_hours_check = None
    CAPTCHA_BLOCK_FILE = "cache/captcha_block.json"
    # With no usable session the bot waits for one to be pasted on the dashboard
    # instead of prompting on the console; how often it re-checks the file, and
    # how often it repeats the instructions while waiting.
    COOKIE_POLL_SECONDS = 10
    COOKIE_REMIND_SECONDS = 300
    # (connect, read) seconds. requests has no default timeout, so a connection
    # that hangs after the server starts throttling/blocking (common right
    # around a bot-protection trigger) would otherwise block the main loop
    # forever with no exception and no captcha marker - it just looks "hung"
    # with a stale heartbeat. Matches the timeout already used for the other
    # direct requests calls in this codebase (twb.py, game/incomings.py).
    REQUEST_TIMEOUT = (10, 30)
    # -- request accounting (instrumentation only, changes no behaviour) -----
    # Written so the captcha tracker can answer one question the logs cannot:
    # is bot protection triggered per REQUEST or per unit of TIME? The two are
    # indistinguishable while the bot runs at a steady pace, so the tracker
    # needs periods where the request rate differs (nights, stalls, between
    # passes) and an exact request count for each - not the 5s-per-request
    # estimate the log timestamps allow.
    #
    # Counters are class-level on purpose: background pollers build their own
    # WebWrapper, and what matters is every request this process makes.
    REQUEST_TRACK_FILE = "cache/request_track.jsonl"
    # One row per this many requests: ~4 minutes apart at the usual pace, so a
    # day of running costs about 30KB.
    REQUEST_TRACK_EVERY = 50
    TRACK_SESSION = int(time.time())
    request_count = 0
    request_by_screen = {}
    _tracked_at = 0

    # Only the wrapper that owns the login (the main loop) persists its rotated
    # cookies back to cache/session.json. Background pollers read that file but
    # must never write it, or two sessions would fight over the session id.
    is_session_owner = False
    _last_persisted_cookies = None

    def __init__(self, url, server=None, endpoint=None, reporter_enabled=False, reporter_constr=None):
        """
        Construct the session and detect variables
        """
        self.web = requests.session()
        self.auth_endpoint = url
        self.server = server
        self.endpoint = endpoint
        self.reporter = ReporterObject(enabled=reporter_enabled, connection_string=reporter_constr)

    def post_process(self, response):
        """
        Post-processes all requests and stores data used for the next request
        """
        xsrf = re.search('<meta content="(.+?)" name="csrf-token"', response.text)
        if xsrf:
            self.headers['x-csrf-token'] = xsrf.group(1)
            self.logger.debug("Set CSRF token")
        elif 'x-csrf-token' in self.headers:
            del self.headers['x-csrf-token']
        self.headers['Referer'] = response.url
        self.last_response = response
        get_h = re.search(r'&h=(\w+)', response.text)
        if get_h:
            self.last_h = get_h.group(1)
        if self.is_session_owner:
            self.persist_session()

    def persist_session(self):
        """Write the live cookie jar back to cache/session.json.

        TribalWars rotates the session id over time; this requests session
        follows that automatically, but the incoming-attack poller loads its
        cookies from cache/session.json. Persisting the rotated cookies here
        keeps the poller on the *current* session instead of replaying a stale
        id - which TribalWars treats as a session conflict and logs out, taking
        this session down with it. Only writes when the cookies actually change.
        """
        cookies = {c.name: c.value for c in self.web.cookies}
        if not cookies or cookies == self._last_persisted_cookies:
            return
        FileManager.save_json_file_atomic({
            'endpoint': self.endpoint,
            'server': self.server,
            'cookies': cookies,
        }, "cache/session.json")
        self._last_persisted_cookies = cookies

    @classmethod
    def _track_write(cls, row):
        """Append one row to the tracker file. Never raises: this is
        instrumentation, and it must not be able to take a request down."""
        try:
            row["ts"] = int(time.time())
            row["session"] = cls.TRACK_SESSION
            path = FileManager.get_path(cls.REQUEST_TRACK_FILE)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:
            pass

    @classmethod
    def _track_request(cls, url):
        """Count one request, bucketed by the game screen it went to."""
        try:
            cls.request_count += 1
            found = re.search(r"[?&]screen=(\w+)", url or "")
            screen = found.group(1) if found else "other"
            cls.request_by_screen[screen] = cls.request_by_screen.get(screen, 0) + 1
            if cls.request_count - cls._tracked_at >= cls.REQUEST_TRACK_EVERY:
                cls._tracked_at = cls.request_count
                cls._track_write({"kind": "tick", "requests": cls.request_count,
                                  "by_screen": dict(cls.request_by_screen)})
        except Exception:
            pass

    @classmethod
    def track_event(cls, kind):
        """Stamp a captcha hit/clear against the exact request count so far."""
        cls._track_write({"kind": kind, "requests": cls.request_count,
                          "by_screen": dict(cls.request_by_screen)})

    def get_url(self, url, headers=None):
        """
        Fetches a URL using a basic GET request
        """
        self.headers['Origin'] = (self.endpoint if self.endpoint else self.auth_endpoint).rstrip('/')
        if not self.priority_mode:
            time.sleep(random.randint(int(3 * self.delay), int(7 * self.delay)))
        url = urljoin(self.endpoint if self.endpoint else self.auth_endpoint, url)
        if not headers:
            headers = self.headers
        try:
            res = self.web.get(url=url, headers=headers, timeout=self.REQUEST_TIMEOUT)
            self._track_request(url)
            self.logger.debug("GET %s [%d]", url, res.status_code)
            self.post_process(res)
            if 'data-bot-protect="forced"' in res.text and not self.block_on_captcha:
                self.logger.warning("Bot protection hit during background poll, skipping")
                return res
            if 'data-bot-protect="forced"' in res.text:
                return self._await_captcha_clear(url, headers)
            return res
        except Exception as e:
            self.logger.warning("GET %s: %s", url, str(e))
            return None

    def _captcha_poll_interval(self, previous=None):
        """How long to wait before the next captcha re-check.

        Fast (CAPTCHA_POLL_SECONDS) while the account is supposed to be awake,
        because that is when someone is around to solve it and every second of
        lag is lost running time. Slow (CAPTCHA_POLL_SECONDS_QUIET) once it is
        supposed to be asleep, where a fast poll buys nothing: the main loop
        only wakes to go back to sleep. Re-evaluated every iteration, so a block
        that spans the window boundary speeds back up on its own at dawn.

        `previous` is the last interval used, purely so the change is logged
        once rather than on every pass.
        """
        interval = self.CAPTCHA_POLL_SECONDS
        check = self.quiet_hours_check
        if check and self.CAPTCHA_POLL_SECONDS_QUIET:
            try:
                if check():
                    interval = self.CAPTCHA_POLL_SECONDS_QUIET
            except Exception as exc:
                # Never let the activity-window lookup be the thing that breaks
                # out of a captcha wait - fall back to the normal cadence.
                self.logger.debug("Quiet-hours check failed: %s", exc)
        if interval != previous:
            if interval == self.CAPTCHA_POLL_SECONDS:
                self.logger.info(
                    "Re-checking for a captcha solve every %ss.", interval)
            else:
                self.logger.info(
                    "Outside active hours - re-checking for a captcha solve "
                    "every %ss instead of %ss. The solve cannot be acted on "
                    "before the window reopens anyway.",
                    interval, self.CAPTCHA_POLL_SECONDS)
        return interval

    def _await_captcha_clear(self, url, headers):
        """Wait out a bot-protection ("forced" captcha) block on the main loop.

        Instead of blocking on input() forever - which needs a keypress in the
        bot's own console and cannot see a solve done in a browser - poll the page
        until the captcha is gone (solved in a browser on the *same* session) and
        resume automatically. A cache/captcha_block.json marker drives the
        dashboard "bot stalled" banner and is removed the moment the block clears,
        so the message goes away on its own with no restart. Deleting that marker
        externally (e.g. a dashboard "Resume" action) just triggers an immediate
        re-check; if the page is still forced the marker is re-armed so the banner
        stays honest.
        """
        # Stamped before anything else so the request count is the one that
        # was standing when the captcha fired. The re-check polls below are
        # deliberately not counted: they cannot trigger a fresh captcha, and
        # counting them would inflate every blocked interval.
        self.track_event("captcha_hit")
        self.logger.warning(
            "Bot protection hit! Solve the captcha in a browser on the same "
            "session - the bot will auto-resume when it clears.")
        self.reporter.report(
            0, "TWB_RECAPTCHA",
            "Bot protection hit. Solve the captcha in a browser on the same "
            "session; the bot resumes automatically.")
        Notification.send(
            "Bot protection hit! Solve the captcha in a browser on the same "
            "session - the bot auto-resumes when it clears.",
            category="captcha")
        FileManager.save_json_file_atomic(
            {"since": int(time.time())}, self.CAPTCHA_BLOCK_FILE)
        interval = None
        while True:
            interval = self._captcha_poll_interval(previous=interval)
            time.sleep(interval)
            try:
                res = self.web.get(url=url, headers=self.headers, timeout=self.REQUEST_TIMEOUT)
                self.post_process(res)
            except Exception as e:
                self.logger.warning("Captcha re-check failed: %s", e)
                continue
            if 'data-bot-protect="forced"' not in res.text:
                FileManager.remove_file(self.CAPTCHA_BLOCK_FILE)
                # The heartbeat was last stamped at the top of the cycle, before
                # the captcha hit; without a refresh the dashboard flips from
                # "captcha" straight to "no heartbeat - may be hung" while the
                # resumed cycle is still finishing.
                heartbeat = FileManager.load_json_file("cache/heartbeat.json") or {}
                heartbeat["ts"] = int(time.time())
                FileManager.save_json_file_atomic(heartbeat, "cache/heartbeat.json")
                self.track_event("captcha_clear")
                self.logger.info("Captcha cleared - resuming.")
                Notification.send("Captcha cleared, bot resumed.", category="captcha")
                return res
            # Still blocked: keep the banner marker present even if it was cleared
            # manually before the captcha was actually solved.
            if FileManager.load_json_file(self.CAPTCHA_BLOCK_FILE) is None:
                FileManager.save_json_file_atomic(
                    {"since": int(time.time())}, self.CAPTCHA_BLOCK_FILE)

    def post_url(self, url, data, headers=None):
        """
        Sends a basic POST request with urlencoded postdata
        """
        if not self.priority_mode:
            time.sleep(
                random.randint(int(3 * self.delay), int(7 * self.delay))
            )
        self.headers['Origin'] = (self.endpoint if self.endpoint else self.auth_endpoint).rstrip('/')
        url = urljoin(self.endpoint if self.endpoint else self.auth_endpoint, url)
        enc = urlencode(data)
        if not headers:
            headers = self.headers
        try:
            res = self.web.post(url=url, data=data, headers=headers, timeout=self.REQUEST_TIMEOUT)
            self._track_request(url)
            self.logger.debug("POST %s %s [%d]", url, enc, res.status_code)
            self.post_process(res)
            if 'data-bot-protect="forced"' in res.text and not self.block_on_captcha:
                self.logger.warning("Bot protection hit during background poll, skipping")
                return res
            if 'data-bot-protect="forced"' in res.text:
                # Building/recruiting/scavenging actions go through here too;
                # without this check a captcha hit on a POST was invisible to
                # the dashboard (no "captcha" banner, just a silent no-op).
                return self._await_captcha_clear(url, headers)
            return res
        except Exception as e:
            self.logger.warning("POST %s %s: %s", url, enc, str(e))
            return None

    def start(self):
        """Start the bot, waiting for a usable session if there is none yet.

        The cookie is deliberately never read from the console. A console's line
        input is far shorter than a TribalWars cookie header, so pasting one into
        cmd.exe silently truncates it: the bot accepts the string, the server
        does not, and every cycle from then on reports "cookie expired" with
        nothing to explain why. The same prompt is also unanswerable for a bot
        started from the dashboard, whose stdin is closed - input() there raises
        EOFError and crashes the run.

        So the session comes from cache/ only, and when there is none we wait for
        one to appear rather than prompting. The dashboard's Overview page has no
        length limit and writes both files, and the bot picks the paste up within
        seconds - no restart.

        Read through FileManager so the files resolve to the active world's data
        dir (worlds/<name>/cache/), not just the project-root cache/.
        """
        session_data = FileManager.load_json_file("cache/session.json")
        if session_data and session_data.get("cookies"):
            if self._session_works(session_data["cookies"]):
                self.logger.info("Game Endpoint: %s", self.endpoint)
                return True
            self.logger.warning("Current session cache not valid")

        raw = FileManager.read_file("cache/cookies.txt")
        if raw and self._session_works(self._parse_cookie_string(raw)):
            print("Loaded cookies from cache/cookies.txt")
            self.logger.info("Game Endpoint: %s", self.endpoint)
            return True

        return self._wait_for_session(tried=raw)

    def _session_works(self, cookies):
        """Load cookies into the jar; True if they give a logged-in game page.

        Nothing is persisted until the cookies are proven good, so a dead paste
        cannot overwrite a working cache/session.json.
        """
        if not cookies:
            return False
        self.web.cookies.clear()
        self.web.cookies.update(cookies)
        self._last_persisted_cookies = None
        was_owner = self.is_session_owner
        self.is_session_owner = False
        test = self.get_url("game.php?screen=overview")
        self.is_session_owner = was_owner
        if not test or "game.php" not in test.url:
            return False
        self.is_session_owner = True
        self.persist_session()
        return True

    def _wait_for_session(self, tried=None):
        """Block until a working cookie string shows up in cache/cookies.txt."""
        path = FileManager.get_path("cache/cookies.txt")
        message = (
            "No usable session yet.\n"
            "Open the dashboard (http://localhost:5000/ by default), paste your "
            "browser cookie string into the Session box on the Overview page and "
            "press 'Update session'.\n"
            "Do NOT paste it into this window - a console cuts long lines short, "
            "which is what makes a cookie look accepted but come back logged "
            "out.\n"
            "Waiting for %s - the bot starts by itself the moment it arrives."
            % path
        )
        print(message)
        self.logger.warning("Waiting for a session (paste the cookie on the dashboard)")
        Notification.send(
            "TWB is waiting for a session: paste your cookie string on the "
            "dashboard's Overview page.", category="session")
        last_reminder = time.time()
        while True:
            time.sleep(self.COOKIE_POLL_SECONDS)
            raw = FileManager.read_file("cache/cookies.txt")
            if raw and raw != tried:
                tried = raw
                if self._session_works(self._parse_cookie_string(raw)):
                    print("Session accepted - starting.")
                    self.logger.info("Session accepted, Game Endpoint: %s", self.endpoint)
                    Notification.send("TWB session accepted, starting.", category="session")
                    return True
                print("That cookie string did not give a logged-in session. Copy "
                      "the whole 'cookie:' header again and paste it on the "
                      "dashboard.")
                self.logger.warning("Pasted cookie string did not authenticate")
            if time.time() - last_reminder > self.COOKIE_REMIND_SECONDS:
                print(message)
                last_reminder = time.time()

    @staticmethod
    def _parse_cookie_string(cinp):
        """Turn a 'k=v; k2=v2' browser cookie string into a dict."""
        cookies = {}
        cinp = (cinp or "").strip().replace('\n', '').replace('\r', '')
        for itt in cinp.split(';'):
            itt = itt.strip()
            if not itt or '=' not in itt:
                continue
            kvs = itt.split("=")
            key = kvs[0]
            value = '='.join(kvs[1:])
            if key:
                cookies[key] = value
        return cookies

    def reauth(self):
        """Re-establish the session from cache/cookies.txt without prompting.

        The main loop calls this when the overview comes back logged out, so a
        fresh cookie string dropped into cache/cookies.txt (by hand or from the
        dashboard) recovers the bot automatically - no restart. Returns True when
        a valid game session is active afterwards.
        """
        # Read through FileManager so the cookie file resolves to the active
        # world's data dir (worlds/<name>/cache/cookies.txt), matching where the
        # dashboard's cookie paste and start() write it. A raw relative open()
        # here would look in the project-root cache/ and never find a
        # per-world cookie, so live re-auth would silently fail.
        raw = FileManager.read_file("cache/cookies.txt")
        if not raw:
            return False
        cookies = self._parse_cookie_string(raw)
        if not cookies:
            return False
        self.web.cookies.clear()
        self.web.cookies.update(cookies)
        # Don't persist the new cookies until we've confirmed they actually work,
        # so a stale cookies.txt can't overwrite session.json with dead cookies.
        was_owner = self.is_session_owner
        self.is_session_owner = False
        test = self.get_url("game.php?screen=overview")
        self.is_session_owner = was_owner
        if test and "game.php" in test.url:
            self.logger.info("Session re-authenticated from cache/cookies.txt")
            self.is_session_owner = True
            self.persist_session()
            return True
        self.logger.warning("Re-auth from cache/cookies.txt failed (cookie still invalid)")
        return False

    def get_action(self, village_id, action):
        """
        Runs an action on a specific village
        """
        url = "game.php?village=%s&screen=%s" % (village_id, action)
        response = self.get_url(url)
        return response

    def get_api_data(self, village_id, action, params={}):

        custom = dict(self.headers)
        custom['accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['x-requested-with'] = "XMLHttpRequest"
        custom['tribalwars-ajax'] = "1"
        req = {
            'ajax': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        res = self.get_url(url, headers=custom)
        # res is None when the underlying request failed (network error, or a
        # captcha page from a background poller). Treat it as "no data" instead
        # of crashing on res.status_code, so the caller retries next cycle.
        if res is not None and res.status_code == 200:
            try:
                return res.json()
            except:
                return res
        return None

    def post_api_data(self, village_id, action, params={}, data={}):
        """
        Simulates an API request
        """
        custom = dict(self.headers)
        custom['accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['x-requested-with'] = "XMLHttpRequest"
        custom['tribalwars-ajax'] = "1"
        req = {
            'ajax': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        if 'h' not in data:
            data['h'] = self.last_h
        res = self.post_url(url, data=data, headers=custom)
        # A failed post_url returns None; guard so the action fails soft (retried
        # next cycle) instead of raising AttributeError on res.status_code.
        if res is not None and res.status_code == 200:
            try:
                return res.json()
            except:
                return res
        return None

    def get_api_action(self, village_id, action, params={}, data={}):
        """
        Simulates an API action being triggered
        """
        custom = dict(self.headers)
        custom['Accept'] = "application/json, text/javascript, */*; q=0.01"
        custom['X-Requested-With'] = "XMLHttpRequest"
        custom['TribalWars-Ajax'] = "1"
        req = {
            'ajaxaction': action,
            'village': village_id,
            'screen': 'api'
        }
        req.update(params)
        payload = f"game.php?{urlencode(req)}"
        url = urljoin(self.endpoint, payload)
        if 'h' not in data:
            data['h'] = self.last_h
        res = self.post_url(url, data=data, headers=custom)
        # A failed post_url returns None; guard so the action fails soft (retried
        # next cycle) instead of raising AttributeError on res.status_code.
        if res is not None and res.status_code == 200:
            try:
                return res.json()
            except:
                return res
        return None
