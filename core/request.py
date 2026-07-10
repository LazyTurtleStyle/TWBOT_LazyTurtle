"""
Class for using one generic cookie jar, emulating a single tab
"""

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
    CAPTCHA_BLOCK_FILE = "cache/captcha_block.json"
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
            res = self.web.get(url=url, headers=headers)
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
        self.logger.warning(
            "Bot protection hit! Solve the captcha in a browser on the same "
            "session - the bot will auto-resume when it clears.")
        self.reporter.report(
            0, "TWB_RECAPTCHA",
            "Bot protection hit. Solve the captcha in a browser on the same "
            "session; the bot resumes automatically.")
        Notification.send(
            "Bot protection hit! Solve the captcha in a browser on the same "
            "session - the bot auto-resumes when it clears.")
        FileManager.save_json_file_atomic(
            {"since": int(time.time())}, self.CAPTCHA_BLOCK_FILE)
        while True:
            time.sleep(self.CAPTCHA_POLL_SECONDS)
            try:
                res = self.web.get(url=url, headers=self.headers)
                self.post_process(res)
            except Exception as e:
                self.logger.warning("Captcha re-check failed: %s", e)
                continue
            if 'data-bot-protect="forced"' not in res.text:
                FileManager.remove_file(self.CAPTCHA_BLOCK_FILE)
                self.logger.info("Captcha cleared - resuming.")
                Notification.send("Captcha cleared, bot resumed.")
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
            res = self.web.post(url=url, data=data, headers=headers)
            self.logger.debug("POST %s %s [%d]", url, enc, res.status_code)
            self.post_process(res)
            return res
        except Exception as e:
            self.logger.warning("POST %s %s: %s", url, enc, str(e))
            return None

    def start(self, ):
        """
        Start the bot and verify whether the last session is still valid
        """
        session_data = FileManager.load_json_file("cache/session.json")
        if session_data:
            self.web.cookies.update(session_data['cookies'])
            self._last_persisted_cookies = dict(session_data['cookies'])
            get_test = self.get_url("game.php?screen=overview")
            if "game.php" in get_test.url:
                self.is_session_owner = True
                return True
            self.logger.warning("Current session cache not valid")

        self.web.cookies.clear()
        # Read through FileManager so the cookie file is found in the active
        # world's data dir (worlds/<name>/cache/cookies.txt), not just the
        # project-root cache/. Only prompt interactively as a last resort.
        cinp = FileManager.read_file("cache/cookies.txt")
        if cinp is not None:
            print("Loaded cookies from cache/cookies.txt")
        else:
            cinp = input("Enter browser cookie string> ")
        cookies = self._parse_cookie_string(cinp)
        self.web.cookies.update(cookies)
        self.logger.info("Game Endpoint: %s", self.endpoint)

        for c in self.web.cookies:
            cookies[c.name] = c.value

        FileManager.save_json_file({
            'endpoint': self.endpoint,
            'server': self.server,
            'cookies': cookies
        }, "cache/session.json")
        self._last_persisted_cookies = dict(cookies)
        self.is_session_owner = True

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
        fresh cookie string dropped into cache/cookies.txt recovers the bot
        automatically - no restart, and no blocking input() like start() uses on
        first run. Returns True when a valid game session is active afterwards.
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
