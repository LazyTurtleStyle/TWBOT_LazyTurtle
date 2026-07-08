"""
Cancel-snipe ("c-snipe") engine.

A cancel snipe puts a village's defense back home inside the milliseconds-wide
gap of an incoming noble train. The mechanic: troops whose command is cancelled
walk home for exactly as long as they had been under way, so a command sent at
S and cancelled at C returns at 2C - S. We send the defense out as an attack on
a (far enough) barbarian village, measure S to the millisecond, cancel at
C = (S + R) / 2, and the stack lands back home at the chosen return moment R -
e.g. 25ms behind the first hit of the train, in front of the nobles.

Constraints, and how they shape the flow:

- Commands can only be cancelled within the world's command_cancel_time
  (usually 10 min) of sending, and the cancel moment is the halfway point, so
  the send must happen within 2x cancel-time of R. The runner claims a snipe
  ``lead_seconds`` (default 18 min) before R and waits out any remaining gate.
- The outgoing target must be far enough away that the troops are still under
  way at C; otherwise they land on the barb and return on their own schedule.
  The server's own travel duration (from the confirm screen) is the
  authoritative check.
- Accuracy: an error in knowing S shifts the return 1:1, an error in timing C
  shifts it 2:1 (return = 2C - S). So S is *measured* rather than assumed - the
  outgoing command's millisecond arrival time minus the server travel duration
  - and the clock is re-synced against the server shortly before the cancel
  fires. Clock sync uses game_state.time_generated (server epoch ms) sampled
  around a request, so precision is roughly half the round-trip time.
- Asymmetry: a return that is EARLY puts the stack home before the hit it was
  dodging (fatal), a late one just lands deeper into the gap (harmless for a
  lone incoming, degraded-but-survivable in a train). Timing errors therefore
  must only ever fall on the late side: the halfway division rounds up, and
  the cancel request is fired at C plus a safety margin with no latency lead,
  so the server processes it at least one-way-latency past C. The achieved
  return lands in [R, R + 2*(safety + latency)] - roughly R..R+400ms on a
  ~100ms connection - never before R.

Mechanics verified live (nl99, 2026-07-08, see the cancel-ms experiments):
a cancelled command's return really is ms-exact 2C - S; second-granularity
folklore ("only the cancel second matters") holds only for UNcancelled trips,
whose turnaround at the target floors to :000. TW renders ms clocks split
across markup ('14:26:49<span>:641</span>'), so clock regexes must run on
tag-stripped text, and command ids are NOT chronological, so our command is
identified by its expected ms arrival rather than by id order.

Storage reuses attack_scheduler's locked, atomic queue-file helpers on a
separate file (cache/csnipes.json) shared between the bot process (executes)
and the web dashboard (arms/disarms). Snipes run sequentially on their own
background thread; overlapping snipes are executed in start order, so arm at
most one per gap.
"""

import html as html_lib
import logging
import re
import time

from bs4 import BeautifulSoup

from core.extractors import Extractor
from core.filemanager import FileManager
from core.notification import Notification
from game import attack_scheduler
from game.incomings import WORLD_CONFIG_CACHE

CSNIPE_FILE = "cache/csnipes.json"

DEFAULT_LEAD_SECONDS = 18 * 60
DEFAULT_GAP_MS = 50
# Fallback cancel window when the world config has not been cached yet.
DEFAULT_CANCEL_WINDOW = 600
# The outgoing troops must still be at least this long under way at the cancel
# moment, or the command would land on the target instead of being cancelled.
MIN_CANCEL_MARGIN_MS = 60_000
# Safety margin kept inside the cancel window (send early enough that C stays
# clearly within command_cancel_time of S).
CANCEL_WINDOW_SLACK_MS = 10_000
# Re-sync the server clock and refresh the cancel link this long before C.
FINAL_SYNC_SECONDS = 25
# Late-bias on the cancel fire moment (adaptive: rtt/2 clamped to this range).
# Covers the clock-offset estimation error so the cancel can never run early;
# every ms of bias shows up as 2ms of extra (safe) lateness on the return.
CANCEL_LATE_BIAS_MIN_MS = 40
CANCEL_LATE_BIAS_MAX_MS = 150
# A snipe stuck in 'running' this long has lost its runner (crash/restart);
# it is marked failed instead of re-executed, since its send may have fired.
STALE_RUNNING_SECONDS = 45 * 60

# Matches an arrival wall clock that includes milliseconds ('23:20:43:387');
# plain countdowns and second-precision clocks have no 4th group and are ignored.
_RE_CLOCK_MS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2}):(\d{1,3})")
# The in-page link that cancels a command (info_command screen).
_RE_CANCEL_URL = re.compile(
    r"href=\"([^\"]*(?:action=cancel|cancel_command|ajax=cancel)[^\"]*)\"")

logger = logging.getLogger("CSnipe")


# -- queue storage (attack_scheduler's locked atomic file, our own path) ------

def _resolve(path):
    return path or FileManager.get_path(CSNIPE_FILE)


def load_snipes(path=None):
    """Current snipe queue (always a list)."""
    return attack_scheduler.load_schedule(path=_resolve(path))


def _update(mutator, path=None):
    return attack_scheduler.update(mutator, path=_resolve(path))


def arm(entry, path=None):
    """Append a new armed snipe and return it (with an assigned id)."""
    entry.setdefault("status", "armed")
    return attack_scheduler.add_command(entry, path=_resolve(path))


def disarm(snipe_id, path=None):
    """Disarm a snipe. Before it runs it is simply dropped from execution;
    while running the runner is asked to cancel the outgoing command right
    away, so the troops come home early instead of at the snipe moment.
    Returns 'disarmed', 'disarm_requested' or None (not found / finished)."""
    def mut(commands):
        for c in commands:
            if c.get("id") != snipe_id:
                continue
            if c.get("status") == "armed":
                c["status"] = "disarmed"
                c["finished"] = int(time.time())
                return "disarmed"
            if c.get("status") == "running":
                c["disarm_requested"] = True
                return "disarm_requested"
        return None
    return _update(mut, path)


def prune(max_age_done=86400, path=None):
    """Drop finished snipes older than max_age_done seconds."""
    now = int(time.time())

    def mut(commands):
        commands[:] = [
            c for c in commands
            if c.get("status") in ("armed", "running")
            or now - int(c.get("finished", c.get("created", now))) < max_age_done
        ]
    _update(mut, path)


def next_start_ts(path=None):
    """Earliest start moment among armed snipes, or None when idle."""
    armed = [c for c in load_snipes(path) if c.get("status") == "armed"]
    if not armed:
        return None
    return min(float(c.get("start_ts", 0)) for c in armed)


def claim_due(path=None, now=None):
    """Atomically move due armed snipes to 'running' and return them. A snipe
    that has been 'running' far longer than any legitimate execution lost its
    runner and is marked failed (its send may already have fired, so it must
    not be re-executed)."""
    now = now if now is not None else time.time()
    claimed = []

    def mut(commands):
        for c in commands:
            status = c.get("status")
            if status == "armed" and float(c.get("start_ts", 0)) <= now:
                c["status"] = "running"
                c["claimed_at"] = int(now)
                claimed.append(dict(c))
            elif (status == "running"
                    and now - int(c.get("claimed_at", 0)) > STALE_RUNNING_SECONDS):
                c["status"] = "failed"
                c["finished"] = int(now)
                c["result"] = "runner died mid-snipe (bot restarted?)"
    _update(mut, path)
    return claimed


def _patch(snipe_id, path=None, **fields):
    def mut(commands):
        for c in commands:
            if c.get("id") == snipe_id:
                c.update(fields)
    _update(mut, path)


def _get(snipe_id, path=None):
    for c in load_snipes(path):
        if c.get("id") == snipe_id:
            return c
    return None


def _event(snipe_id, message, path=None):
    """Append a timestamped line to the snipe's event log (shown on the tab)."""
    logger.info("[%s] %s", snipe_id, message)
    stamp = time.strftime("%H:%M:%S")

    def mut(commands):
        for c in commands:
            if c.get("id") == snipe_id:
                events = c.setdefault("events", [])
                events.append("%s %s" % (stamp, message))
                del events[:-30]
    _update(mut, path)


def _finish(snipe_id, status, result, path=None, notify=True, **fields):
    _patch(snipe_id, path=path, status=status, result=result,
           finished=int(time.time()), **fields)
    _event(snipe_id, "%s: %s" % (status, result), path=path)
    if notify:
        Notification.send("TWB c-snipe %s: %s" % (status, result))


# -- server clock ------------------------------------------------------------

class _Clock:
    """Server-clock offset estimator.

    Samples game_state.time_generated (server epoch ms) around a page GET; the
    offset is measured against the request's local midpoint, so the estimate is
    accurate to about half the round-trip time. `rtt` is kept so request
    launches can lead by the one-way latency."""

    def __init__(self):
        self.offset_ms = None
        self.rtt = 0.2

    def sync(self, wrapper, url):
        """GET url, refresh offset/rtt from its game state, return the response."""
        t0 = time.time()
        res = wrapper.get_url(url)
        t1 = time.time()
        if res is not None:
            state = Extractor.game_state(res)
            if state and state.get("time_generated"):
                self.offset_ms = float(state["time_generated"]) - (t0 + t1) / 2.0 * 1000.0
                self.rtt = max(0.0, t1 - t0)
        return res

    def server_now_ms(self):
        return time.time() * 1000.0 + self.offset_ms

    def sleep_until(self, server_ms, network_lead=0.0, lead=True):
        """Sleep so that a request fired on return is *processed* at server_ms:
        lead by the one-way latency (rtt/2) plus any configured extra.

        With lead=False the request instead FIRES at server_ms (local clock),
        so the server processes it at least one one-way latency AFTER
        server_ms - used for the cancel, which must never run early."""
        target_local = (server_ms - self.offset_ms) / 1000.0 - network_lead
        if lead:
            target_local -= self.rtt / 2.0
        wait = target_local - time.time()
        if wait > 0:
            time.sleep(wait)
        return wait


# -- page parsing ------------------------------------------------------------

def _page_text(html):
    """Tag-stripped page text for clock parsing. TW splits ms clocks across
    markup ('14:26:49<span class="small grey">:641</span>'), so the regex must
    run on text with the tags removed and NO separator inserted - anything
    between the seconds and the ms breaks the match."""
    return BeautifulSoup(html or "", "html.parser").get_text()


def _match_clock_ms(text, expected_ms):
    """Find the millisecond wall clock in `text` that corresponds to the epoch
    we expect within a few seconds, and return its exact epoch ms.

    Pages carry several clocks (countdowns, the server-time footer); only
    H:MM:SS:mmm forms are considered, and the right one is identified by its
    second-of-minute relative to `expected_ms` - which also makes the
    conversion timezone-proof, exactly like the incomings parser."""
    expected_s = int(expected_ms // 1000)
    best = None
    for match in _RE_CLOCK_MS.finditer(text or ""):
        second, millis = int(match.group(3)), int(match.group(4))
        epoch_s = expected_s + ((second - expected_s % 60 + 30) % 60 - 30)
        epoch_ms = epoch_s * 1000 + millis
        if abs(epoch_ms - expected_ms) <= 3000:
            if best is None or abs(epoch_ms - expected_ms) < abs(best - expected_ms):
                best = epoch_ms
    return best


def _extract_cancel_url(text):
    match = _RE_CANCEL_URL.search(text or "")
    return html_lib.unescape(match.group(1)) if match else None


def _locate_outgoing(wrapper, clock, village_id, target_x, target_y,
                     expected_arrival_ms):
    """Find our just-sent command and return (command_id, arrival_ms, cancel_url).

    The village overview lists outgoing commands as info_command links whose
    row text carries the target coordinates and a ms arrival clock. Ours is
    the row whose arrival matches the expected one (send + server travel time,
    +-3s) - command ids are NOT chronological, so id order cannot identify it;
    it only serves as a fallback when no row clock parses. The info_command
    page then confirms the millisecond arrival and gives the cancel link. Any
    of the three may come back None - callers degrade gracefully."""
    res = clock.sync(wrapper, "game.php?village=%s&screen=overview" % village_id)
    if res is None:
        return None, None, None
    coords = "(%s|%s)" % (target_x, target_y)
    matched, unmatched = [], []
    soup = BeautifulSoup(res.text, "html.parser")
    for link in soup.find_all("a", href=re.compile(r"screen=info_command")):
        row = link.find_parent("tr")
        if not row or coords not in row.get_text():
            continue
        cid = re.search(r"id=(\d+)", link.get("href", ""))
        if not cid:
            continue
        command_id = int(cid.group(1))
        arrival = _match_clock_ms(row.get_text(), expected_arrival_ms)
        if arrival is not None:
            matched.append((abs(arrival - expected_arrival_ms), command_id))
        else:
            unmatched.append(command_id)
    candidates = [cid for _, cid in sorted(matched)]
    candidates += [c for c in sorted(set(unmatched), reverse=True)
                   if c not in candidates]
    for command_id in candidates[:3]:
        page = clock.sync(
            wrapper,
            "game.php?village=%s&screen=info_command&id=%d&type=own"
            % (village_id, command_id))
        if page is None or coords not in page.text:
            continue
        arrival_ms = _match_clock_ms(_page_text(page.text), expected_arrival_ms)
        cancel_url = _extract_cancel_url(page.text)
        if arrival_ms is not None or cancel_url:
            return command_id, arrival_ms, cancel_url
    return None, None, None


# -- execution ---------------------------------------------------------------

def _cancel_window_ms():
    config = FileManager.load_json_file(WORLD_CONFIG_CACHE) or {}
    return int(config.get("command_cancel_time") or DEFAULT_CANCEL_WINDOW) * 1000


def _wait_checking_disarm(snipe_id, clock, until_server_ms, path):
    """Coarse-sleep until the given server moment, polling the queue so a
    dashboard disarm is noticed. Returns True if a disarm was requested."""
    while clock.server_now_ms() < until_server_ms:
        entry = _get(snipe_id, path)
        if entry is None or entry.get("disarm_requested") \
                or entry.get("status") not in ("running",):
            return True
        time.sleep(min(2.0, max(0.05,
                   (until_server_ms - clock.server_now_ms()) / 1000.0)))
    return False


def execute(wrapper, snipe, path=None, network_lead=0.0):
    """Run one claimed snipe to completion: send, measure, cancel.

    Timeline (all server epoch ms): R = earliest acceptable return; S = send,
    aligned to R's millisecond offset so a fallback cancel-at-half stays exact
    when measuring S fails; C = ceil((S + R) / 2) = cancel, fired with a late
    bias so the achieved return lands in [R, R + 2*(safety + latency)]."""
    sid = snipe.get("id")
    village_id = snipe.get("village_id")
    return_ms = int(snipe.get("return_ms", 0))

    clock = _Clock()
    if clock.sync(wrapper, "game.php?village=%s&screen=overview" % village_id) is None \
            or clock.offset_ms is None:
        return _finish(sid, "failed", "could not sync the server clock "
                       "(session dead?)", path=path)
    _event(sid, "clock synced (offset %+dms, rtt %dms)"
           % (clock.offset_ms, clock.rtt * 1000), path=path)

    cancel_window = _cancel_window_ms()
    # Gate: the send may happen at most (2 x cancel window) before R, or the
    # halfway cancel moment would fall outside the window. Claimed early
    # (small-cancel-time worlds), we wait the difference out first.
    send_floor = return_ms - (2 * cancel_window - CANCEL_WINDOW_SLACK_MS)
    if clock.server_now_ms() < send_floor - 5000:
        _event(sid, "waiting for the cancel-window gate (world cancel time %ds)"
               % (cancel_window // 1000), path=path)
        if _wait_checking_disarm(sid, clock, send_floor - 5000, path):
            return _finish(sid, "disarmed", "disarmed before send", path=path,
                           notify=False)

    # Prepare the outgoing command (rally point open + confirm). The server's
    # travel duration from the confirm page is authoritative.
    confirm_data, duration, err = attack_scheduler.prepare_command(
        wrapper, village_id, snipe.get("target_x"), snipe.get("target_y"),
        snipe.get("units") or {})
    if err:
        return _finish(sid, "failed", err, path=path)

    now = clock.server_now_ms()
    if return_ms - now < 20_000:
        return _finish(sid, "failed", "armed/claimed too late - the train "
                       "lands in under 20s", path=path)
    # First send moment >= now+2s (and >= the gate) sharing R's ms offset.
    earliest = max(now + 2000, send_floor)
    send_target = earliest - (earliest % 1000) + (return_ms % 1000)
    if send_target < earliest:
        send_target += 1000

    cancel_at = (send_target + return_ms + 1) // 2
    if duration * 1000 < (cancel_at - send_target) + MIN_CANCEL_MARGIN_MS:
        return _finish(
            sid, "failed",
            "target too close: troops would arrive in %ds but must still be "
            "under way at the cancel moment %ds after the send - pick a barb "
            "further out" % (duration, (cancel_at - send_target) // 1000),
            path=path)

    entry = _get(sid, path)
    if entry is None or entry.get("disarm_requested"):
        return _finish(sid, "disarmed", "disarmed before send", path=path,
                       notify=False)

    _patch(sid, path=path, send_target_ms=int(send_target),
           travel_seconds=int(duration))
    _event(sid, "sending in %.1fs (aimed at .%03d)"
           % ((send_target - now) / 1000.0, send_target % 1000), path=path)
    clock.sleep_until(send_target, network_lead)
    ok, msg = attack_scheduler.fire_command(wrapper, village_id, confirm_data)
    if not ok:
        return _finish(sid, "failed", "launch request failed - troops did NOT "
                       "leave", path=path)

    # Measure the true send moment: the outgoing command's millisecond arrival
    # minus the (whole-second) server travel duration. Falls back to the aimed
    # moment when the page cannot be read.
    command_id, arrival_out_ms, cancel_url = _locate_outgoing(
        wrapper, clock, village_id, snipe.get("target_x"),
        snipe.get("target_y"), send_target + duration * 1000)
    if arrival_out_ms is not None:
        send_actual = arrival_out_ms - duration * 1000
        _event(sid, "send measured at .%03d (aimed .%03d)"
               % (send_actual % 1000, send_target % 1000), path=path)
    else:
        # Unmeasured send: assume it fired LATE by a full round-trip. An
        # overestimated S only delays the return (safe); an underestimated one
        # brings it home early (fatal) - observed sends run tens of ms late.
        send_actual = send_target + int(clock.rtt * 1000)
        _event(sid, "could not read the outgoing command's ms arrival; "
               "assuming the aimed send moment +%dms (late-safe)"
               % int(clock.rtt * 1000), path=path)
    if not cancel_url:
        return _finish(sid, "failed", "no cancel link found on the outgoing "
                       "command - troops will hit the target and return on "
                       "their own", path=path, outgoing_id=command_id)

    # Ceil: rounding the halfway moment down would shave the return early.
    cancel_at = (send_actual + return_ms + 1) // 2
    _patch(sid, path=path, send_ms=int(send_actual), outgoing_id=command_id,
           cancel_ms=int(cancel_at))
    _event(sid, "cancel scheduled at .%03d (in %.1fs)"
           % (cancel_at % 1000, (cancel_at - clock.server_now_ms()) / 1000.0),
           path=path)

    # Wait out most of the gap; a disarm now means "bring them home ASAP".
    if _wait_checking_disarm(sid, clock,
                             cancel_at - FINAL_SYNC_SECONDS * 1000, path):
        entry = _get(sid, path) or {}
        if entry.get("status") != "running":  # deleted externally; still cancel
            _event(sid, "queue entry vanished; cancelling immediately", path=path)
        cancel_at = clock.server_now_ms() + 1500
        _event(sid, "disarm requested - cancelling now, troops return early",
               path=path)
        disarmed = True
    else:
        disarmed = False
        # Final re-sync close to C: refresh the clock offset and the cancel
        # link (same page), so drift over the multi-minute wait is gone.
        if command_id:
            page = clock.sync(
                wrapper, "game.php?village=%s&screen=info_command&id=%d&type=own"
                % (village_id, command_id))
            fresh = _extract_cancel_url(page.text) if page is not None else None
            if fresh:
                cancel_url = fresh

    # Fire the cancel with NO latency lead and a safety margin on top: server
    # processing then lands at least one-way-latency past C and clock-offset
    # error stays covered, so the cancel - and with it the return, at double
    # weight - can only ever err late, never early (early = home before the
    # hit = dead stack).
    safety_ms = 0
    if not disarmed:
        safety_ms = int(min(CANCEL_LATE_BIAS_MAX_MS,
                            max(CANCEL_LATE_BIAS_MIN_MS, clock.rtt * 500.0)))
        # Ceiling: lateness = 2*(safety + one-way latency), and asymmetric
        # routing can push one-way toward the full rtt.
        _event(sid, "cancel fires at .%03d +%dms late-bias (return lands "
               "%d-%dms past target)"
               % (cancel_at % 1000, safety_ms, 2 * safety_ms,
                  2 * (safety_ms + int(clock.rtt * 1000))), path=path)
    clock.sleep_until(cancel_at + safety_ms, lead=False)
    t0 = time.time()
    res = wrapper.get_url(cancel_url)
    if res is None:  # one immediate retry: a late cancel beats no cancel
        res = wrapper.get_url(cancel_url)
    if res is None:
        return _finish(sid, "failed", "cancel request failed - troops will "
                       "hit the target and return on their own", path=path)

    # Report what actually happened. The response's own generation time is the
    # server-side processing moment of the cancel, so the achieved return is
    # 2C - S even when the page's return-arrival clock cannot be parsed.
    state = Extractor.game_state(res)
    cancel_actual = float(state["time_generated"]) if state \
        and state.get("time_generated") else (t0 * 1000 + clock.offset_ms)
    return_actual = int(2 * cancel_actual - send_actual)
    parsed = _match_clock_ms(_page_text(res.text), return_actual)
    if parsed is not None:
        return_actual = parsed
    delta = return_actual - return_ms
    status = "disarmed" if disarmed else "done"
    summary = ("troops return at .%03d, %+dms vs target"
               % (return_actual % 1000, delta)) if not disarmed else \
        "cancelled early on request; troops return ahead of the snipe moment"
    _finish(sid, status, summary, path=path, notify=not disarmed,
            return_actual_ms=int(return_actual), delta_ms=int(delta))


def run_due(wrapper, path=None, network_lead=0.0):
    """Claim and execute every due snipe, soonest first. Returns the count."""
    executed = 0
    for snipe in sorted(claim_due(path=path),
                        key=lambda s: float(s.get("start_ts", 0))):
        try:
            execute(wrapper, snipe, path=path, network_lead=network_lead)
        except Exception as exc:  # never let one bad snipe kill the thread
            logger.exception("c-snipe %s crashed", snipe.get("id"))
            _finish(snipe.get("id"), "failed", "exception: %s" % exc, path=path)
        executed += 1
    return executed
