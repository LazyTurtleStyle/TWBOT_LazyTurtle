"""The game server's own wall clock, learned from its page furniture.

Every game page prints its clock at the bottom:

    Servertijd: <span id="serverTime">12:02:58</span> <span id="serverDate">14/07/2026</span>

Comparing that to the host's clock at the moment the page arrived gives the
offset between the two. One number covers both ways the clocks can disagree:

* the host clock has drifted, or
* the host simply runs in a different timezone than the world does.

The second case is the dangerous one, because the epoch timestamps both sides
exchange are identical - nothing looks wrong - while every wall-clock string a
player writes down (a forced-peace window, an arrival time) means a different
instant to the bot than it did to the player reading the game's own clock.

Anything converting a user-written wall clock into a moment should go through
`ServerClock.now()` / `ServerClock.to_server()` rather than `datetime.now()`.
"""

import datetime
import logging
import re
import time

from core.filemanager import FileManager

CACHE_PATH = "cache/server_clock.json"

# The offset is a timezone difference plus a little clock drift, so anything
# beyond half a day means we misread the date (markets order it differently)
# and the sample must be thrown away rather than trusted.
MAX_PLAUSIBLE_OFFSET = 14 * 3600

_TIME_RE = re.compile(r'id=["\']serverTime["\'][^>]*>\s*([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)\s*<', re.I)
_DATE_RE = re.compile(r'id=["\']serverDate["\'][^>]*>\s*([0-9]{1,4}[./-][0-9]{1,2}[./-][0-9]{1,4})\s*<', re.I)

# Markets differ in how they order the date; the offset sanity check below picks
# the reading that actually lines up with the host clock.
_DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%y")

logger = logging.getLogger("twb.server_clock")


class ServerClock:
    _offset = None  # seconds to add to host time to get server wall clock

    @staticmethod
    def parse(html):
        """Server wall clock as a naive datetime, or None if the page lacks it."""
        if not html:
            return None
        time_match = _TIME_RE.search(html)
        date_match = _DATE_RE.search(html)
        if not time_match or not date_match:
            return None
        raw_time = time_match.group(1)
        if raw_time.count(":") == 1:
            raw_time += ":00"
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.datetime.strptime(
                    "%s %s" % (date_match.group(1), raw_time), fmt + " %H:%M:%S")
            except ValueError:
                continue
            # A mm/dd page read as dd/mm lands days away; only a reading within
            # timezone range of the host can be the right one.
            offset = (parsed - datetime.datetime.now()).total_seconds()
            if abs(offset) <= MAX_PLAUSIBLE_OFFSET:
                return parsed
        return None

    @classmethod
    def sample(cls, html):
        """Learn the offset from a freshly fetched page. Returns it, or None.

        Called on every overview fetch rather than once, so a daylight-saving
        change on either side is picked up the same day instead of skewing
        every window until the next restart.
        """
        server_now = cls.parse(html)
        if server_now is None:
            return None
        offset = (server_now - datetime.datetime.now()).total_seconds()
        cls._offset = offset
        try:
            FileManager.save_json_file_atomic({
                "offset_seconds": round(offset, 3),
                "server_time": server_now.strftime("%Y-%m-%d %H:%M:%S"),
                "sampled_at": int(time.time()),
            }, CACHE_PATH)
        except OSError:  # a cache we can't write is not worth crashing a run over
            logger.debug("Could not persist the server clock offset", exc_info=True)
        return offset

    @classmethod
    def offset(cls):
        """Seconds to add to host time to get server wall clock (0.0 if unknown).

        Falling back to 0.0 means "assume the host matches the server", which is
        exactly the old behaviour - so a world whose clock we never managed to
        read behaves as it always did.
        """
        if cls._offset is not None:
            return cls._offset
        try:
            cached = FileManager.load_json_file(CACHE_PATH)
        except Exception:
            cached = None
        if cached and isinstance(cached.get("offset_seconds"), (int, float)):
            cls._offset = float(cached["offset_seconds"])
            return cls._offset
        return 0.0

    @classmethod
    def now(cls):
        """Current moment as the game server's wall clock reads it."""
        return datetime.datetime.now() + datetime.timedelta(seconds=cls.offset())

    @classmethod
    def to_server(cls, moment):
        """Convert a host-local datetime (or unix timestamp) to server wall clock."""
        if not isinstance(moment, datetime.datetime):
            moment = datetime.datetime.fromtimestamp(moment)
        return moment + datetime.timedelta(seconds=cls.offset())
