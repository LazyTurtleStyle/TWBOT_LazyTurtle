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
- Return arithmetic (measured live on an NL world, 2026-07-08, via the test tab): the
  server credits a cancelled command's under-way time in WHOLE seconds - the
  troops land back at S + 2k seconds (k = whole seconds under way at the
  cancel), keeping the send's millisecond offset exactly. The ms-precise
  "2C - S" arrival the cancel-response page displays is a rendering artifact;
  the command list afterwards (and the troops) follow the quantized value, so
  the achieved return is read back from the command list, never from the
  cancel page. Natural (uncancelled) returns floor the turnaround at the
  target to :000 - the same whole-second crediting.
- Consequences for accuracy: the send's ms IS the return's ms, so the send is
  aimed at R's ms offset plus a small late buffer on the correct 2-SECOND
  parity (return - send must be an even number of seconds), and S is
  *measured* afterwards (the outgoing command's ms arrival minus the server
  travel duration). Send jitter shifts the return 1:1 within the second;
  a send more than the buffer EARLY bumps the return one 2s slot later
  (harmless), never earlier. The jitter is mostly a stable per-connection
  bias (the rtt/2 request lead includes server render time), so throwaway
  probes fired just before the real send measure it live and let the buffer
  shrink from ~150ms to ~40ms: achieved returns run R+~30..90ms calibrated,
  R+150..400ms blind.
- Asymmetry: a return that is EARLY puts the stack home before the hit it was
  dodging (fatal), a late one just lands deeper into the gap. The cancel only
  has to land inside a one-second window, but firing before the window opens
  rolls the return a fatal 2s early - so k is sized from the earliest the
  send may have fired, the cancel fires a margin into the window with no
  latency lead, and overshooting the far edge merely costs 2s of lateness.
  Whether the window is anchored to the send's ms or to wall-clock seconds is
  unresolved; firing after the later of the two starts is safe under both.

Parsing notes: TW renders ms clocks split across markup
('14:26:49<span>:641</span>'), so clock regexes must run on tag-stripped
text, and command ids are NOT chronological, so our command is identified by
its expected ms arrival rather than by id order.

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
from core.server_clock import GameClock
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
# The return keeps the send's ms, so the send is aimed this far past R's ms
# offset: send jitter (observed ~+-100ms) then stays on the late side, and
# only a send earlier than this buffer bumps the return one 2s slot later
# (late = harmless) instead of landing it before the target (fatal).
SEND_LATE_BUFFER_MS = 150
# Fire the cancel this far into its one-second window (adaptive: rtt/2
# clamped to this range). Before the window opens the return rolls 2s early
# - fatal; past the far edge it slips 2s late - harmless.
CANCEL_MARGIN_MIN_MS = 250
CANCEL_MARGIN_MAX_MS = 600
# An unmeasured send may have fired up to this much before the aimed moment;
# k is sized from that early bound (a too-small k returns 2s early).
UNMEASURED_SEND_EARLY_MS = 300
# Send-timing calibration: the rtt/2 request lead includes server render
# time, so sends land a connection-dependent but fairly stable few tens of
# ms off the aim (observed ~-65ms). Throwaway one-unit probes fired at the
# snipe target just before the real send measure today's bias live; with a
# calibrated bias the late buffer shrinks to the probe-spread-based minimum
# below instead of the blind SEND_LATE_BUFFER_MS.
PROBE_COUNT = 2
PROBE_MIN_LEAD_MS = 180_000   # skip probing when the snipe is this close
PROBED_BUFFER_MIN_MS = 40
PROBED_BUFFER_ONE_SAMPLE_MS = 75
# Two-sided window mode (alpha): with a window_ms on the snipe the return may
# land at most that far past R, so a send whose measured ms falls outside it
# is cancelled right away and re-fired on a later 2s slot - repeat-until-in-
# band instead of one-shot jitter luck. Retries stop past this budget or when
# R is closer than this lead (the last attempt still needs locate + the
# halfway cancel); a kept miss is late-safe, never early.
MAX_SEND_ATTEMPTS = 8
RETRY_MIN_LEAD_MS = 45_000
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
        Notification.send("TWB c-snipe %s: %s" % (status, result), category="attack")


# -- server clock ------------------------------------------------------------

# The snipe engine's own clock lives in core.server_clock now: the attack
# scheduler needs the same millisecond aiming, and one implementation of a
# server-clock estimator is enough.
_Clock = GameClock


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


def _plan_cancel(send_low_ms, send_high_ms, return_ms, rtt):
    """Cancel moment under the quantized-return model: the troops land back
    at S + 2k seconds, so pick the smallest k whose return is not before
    return_ms and fire a margin into the one-second window that credits it.
    k is sized from the earliest the send may have fired and the window
    start from the latest - an error in either direction would otherwise
    pull the return a fatal 2s early (pass send_low == send_high for a
    measured send). Returns (fire_at_ms, margin_ms, k)."""
    k = max(1, -(-(return_ms - send_low_ms) // 2000))
    margin = int(min(CANCEL_MARGIN_MAX_MS, max(CANCEL_MARGIN_MIN_MS, rtt * 500)))
    # If the crediting window is anchored to wall-clock seconds instead of
    # the send's ms, everything past the next :000 belongs to k+1 (harmless,
    # +2s); stay under it when the send's ms offset leaves room.
    frac = int(send_high_ms % 1000)
    if frac + margin > 900:
        margin = max(150, 900 - frac)
    return int(send_high_ms + k * 1000 + margin), margin, k


def _probe_send_bias(wrapper, clock, sid, village_id, tx, ty, units, path,
                     network_lead=0.0):
    """Measure today's send-timing bias with throwaway probes.

    Fires up to PROBE_COUNT one-unit attacks at the snipe target, compares
    each processed send moment (ms arrival minus server travel time) against
    its aim, and cancels the probe right away. The probes use one unit of a
    type from the snipe's own selection, so the caller must wait until they
    are home again (second return value) before preparing the real send.
    Returns (offsets list in ms, epoch ms when all probe troops are back)."""
    unit = next(iter(units))
    offsets = []
    home_ms = 0
    for attempt in range(PROBE_COUNT):
        entry = _get(sid, path)
        if entry is None or entry.get("disarm_requested"):
            break
        confirm_data, duration, err = attack_scheduler.prepare_command(
            wrapper, village_id, tx, ty, {unit: 1})
        if err:
            break
        aim = int(clock.server_now_ms() + 3000)
        clock.sleep_until(aim, network_lead)
        ok, _ = attack_scheduler.fire_command(wrapper, village_id, confirm_data)
        if not ok:
            break
        _, arrival, cancel_url = _locate_outgoing(
            wrapper, clock, village_id, tx, ty, aim + duration * 1000)
        cancelled_at = clock.server_now_ms()
        if cancel_url:
            wrapper.get_url(cancel_url)
            # a cancelled probe walks home for as long as it was under way
            home_ms = max(home_ms, cancelled_at + (cancelled_at - aim) + 3000)
        else:
            # no cancel link: the probe lands on the barb and walks back on
            # its own; travel there and back plus slack
            home_ms = max(home_ms, aim + 2000 * duration + 5000)
        if arrival is not None:
            offset = arrival - duration * 1000 - aim
            if abs(offset) <= 400:  # anything bigger is a mismeasurement
                offsets.append(int(offset))
    return offsets, int(home_ms)


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
    aimed at R's ms offset + SEND_LATE_BUFFER_MS on the correct 2-second
    parity (the return lands at S + 2k whole seconds, keeping S's ms); the
    cancel fires a margin into the one-second window crediting the k whose
    return is not before R. Achieved return: R + buffer + residual send
    jitter - typically R+30..90ms with probe calibration, R+150..400ms
    without, +2s when the send misfires early - never < R.

    With window_ms set (two-sided window, alpha) the send is instead aimed at
    the window's middle and re-fired - cancel the miss, wait the troops home,
    try a later 2s slot - until a measured send lands the return inside
    [R, R + window_ms] or the retry budget/lead runs out."""
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

    # Live calibration: the send's ms IS the return's ms and cannot be fixed
    # after firing, so when there is time, measure today's send-timing bias
    # with throwaway probes and shrink the blind late buffer accordingly.
    # The buffer widens with the probes' disagreement: it must absorb the
    # residual jitter left after subtracting the systematic bias.
    send_bias, buffer_ms = 0, SEND_LATE_BUFFER_MS
    if return_ms - clock.server_now_ms() > PROBE_MIN_LEAD_MS:
        _event(sid, "calibrating send timing with %d probe(s)" % PROBE_COUNT,
               path=path)
        offsets, probes_home = _probe_send_bias(
            wrapper, clock, sid, village_id, snipe.get("target_x"),
            snipe.get("target_y"), snipe.get("units") or {}, path,
            network_lead)
        if len(offsets) >= 2:
            send_bias = sum(offsets) // len(offsets)
            spread = max(offsets) - min(offsets)
            buffer_ms = min(SEND_LATE_BUFFER_MS,
                            max(PROBED_BUFFER_MIN_MS, 30 + spread))
        elif len(offsets) == 1:
            send_bias, buffer_ms = offsets[0], PROBED_BUFFER_ONE_SAMPLE_MS
        if offsets:
            _event(sid, "send bias %s ms - aiming %+dms past the target ms "
                   "(instead of the blind +%d)"
                   % ("/".join("%+d" % o for o in offsets),
                      buffer_ms, SEND_LATE_BUFFER_MS), path=path)
        else:
            _event(sid, "send-bias probes failed; keeping the safe +%dms "
                   "buffer" % SEND_LATE_BUFFER_MS, path=path)
        if probes_home and _wait_checking_disarm(sid, clock, probes_home,
                                                 path):
            return _finish(sid, "disarmed", "disarmed before send", path=path,
                           notify=False)

    # Two-sided window (alpha): the return may land at most window_ms past R
    # (both sides fatal, e.g. a tight train gap). Aim mid-window instead of
    # target + buffer, and turn the one-shot send into fire-measure-refire:
    # a send whose measured ms misses the window is cancelled immediately
    # (troops home in seconds) and re-fired on a later 2s slot.
    window_ms = int(snipe.get("window_ms") or 0)

    attempt = 0
    while True:
        attempt += 1
        # Prepare the outgoing command (rally point open + confirm). The
        # server's travel duration from the confirm page is authoritative;
        # every attempt needs a fresh confirm token.
        confirm_data, duration, err = attack_scheduler.prepare_command(
            wrapper, village_id, snipe.get("target_x"), snipe.get("target_y"),
            snipe.get("units") or {})
        if err:
            return _finish(sid, "failed", err, path=path)

        now = clock.server_now_ms()
        if return_ms - now < 20_000:
            return _finish(sid, "failed", "armed/claimed too late - the train "
                           "lands in under 20s", path=path)
        # First send moment >= now+2s (and >= the gate) on the right 2-second
        # parity: the return keeps the send's ms and lands an even number of
        # seconds after it, so aim at R's ms offset + the late buffer (or the
        # window's middle), corrected by the measured bias (the send is
        # expected to land at aim + bias), all mod 2s.
        aim = (return_ms + (window_ms // 2 if window_ms else buffer_ms)
               - send_bias) % 2000
        earliest = max(now + 2000, send_floor)
        send_target = earliest - (earliest % 2000) + aim
        if send_target < earliest:
            send_target += 2000

        cancel_at, _, _ = _plan_cancel(send_target, send_target, return_ms,
                                       clock.rtt)
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
               travel_seconds=int(duration), send_attempts=attempt)
        _event(sid, "sending in %.1fs (aimed at .%03d)%s"
               % ((send_target - now) / 1000.0, send_target % 1000,
                  " - attempt %d" % attempt if attempt > 1 else ""), path=path)
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
            send_low = send_high = send_actual
            _event(sid, "send measured at .%03d (aimed .%03d, %+dms vs aim)"
                   % (send_actual % 1000, send_target % 1000,
                      send_actual - send_target), path=path)
        else:
            # Unmeasured send: bound it both ways. k is sized from the earliest
            # it may have fired and the cancel window from the latest - a wrong
            # guess in either direction would bring the troops home 2s early.
            send_low = send_target - UNMEASURED_SEND_EARLY_MS
            send_high = send_target + int(clock.rtt * 1000)
            send_actual = send_high
            _event(sid, "could not read the outgoing command's ms arrival; "
                   "bounding the send -%d/+%dms around the aim (late-safe)"
                   % (UNMEASURED_SEND_EARLY_MS, int(clock.rtt * 1000)), path=path)
        if not cancel_url:
            return _finish(sid, "failed", "no cancel link found on the outgoing "
                           "command - troops will hit the target and return on "
                           "their own", path=path, outgoing_id=command_id)

        if not window_ms:
            break
        # Window verdict: the return keeps the send's ms, so it lands this far
        # past R (an early-side send quantizes to the next slot = far past).
        past = int((send_actual - return_ms) % 2000)
        if arrival_out_ms is not None and past <= window_ms:
            _event(sid, "send inside the window - return lands +%dms of the "
                   "allowed +%d" % (past, window_ms), path=path)
            break
        if attempt >= MAX_SEND_ATTEMPTS \
                or return_ms - clock.server_now_ms() < RETRY_MIN_LEAD_MS:
            _event(sid, "out of retry budget (attempt %d/%d, %ds left) - "
                   "keeping this send, late-safe"
                   % (attempt, MAX_SEND_ATTEMPTS,
                      int((return_ms - clock.server_now_ms()) / 1000)),
                   path=path)
            break
        # Missed: pull the troops home right away and try a later slot. A
        # measured miss is a fresh bias sample - fold it in so the next aim
        # starts closer.
        if arrival_out_ms is not None:
            send_bias = (send_bias + int(send_actual - send_target)) // 2
            reason = ("return would land +%dms vs target (window +%d)"
                      % (past, window_ms))
        else:
            reason = "send unmeasured - cannot verify the window"
        _event(sid, "attempt %d missed: %s - cancelling and re-firing"
               % (attempt, reason), path=path)
        if wrapper.get_url(cancel_url) is None:
            _event(sid, "cancel of the missed attempt failed; keeping it "
                   "(late-safe) instead of risking a double command", path=path)
            break
        cancelled_at = clock.server_now_ms()
        # a cancelled command walks home for as long as it was under way
        home_ms = cancelled_at + (cancelled_at - send_actual) + 3000
        if _wait_checking_disarm(sid, clock, home_ms, path):
            return _finish(sid, "disarmed", "disarmed between window attempts; "
                           "the outgoing command was already cancelled",
                           path=path, notify=False)

    cancel_at, margin_ms, k = _plan_cancel(send_low, send_high, return_ms,
                                           clock.rtt)
    return_planned = send_low + 2000 * k
    _patch(sid, path=path, send_ms=int(send_actual), outgoing_id=command_id,
           cancel_ms=int(cancel_at), return_planned_ms=int(return_planned))
    _event(sid, "cancel window opens at .%03d, firing +%dms in (in %.1fs) - "
           "return quantizes to .%03d (%+dms vs target)"
           % ((send_high + k * 1000) % 1000, margin_ms,
              (cancel_at - clock.server_now_ms()) / 1000.0,
              return_planned % 1000, return_planned - return_ms), path=path)

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

    # Fire the cancel with NO latency lead: the margin is already inside
    # cancel_at, and server processing lands one-way-latency further into the
    # window - never before it opens (before = the return rolls 2s early =
    # home before the hit = dead stack; past the far edge = 2s late, safe).
    if not disarmed:
        _event(sid, "cancel fires at .%03d (%dms into the return window)"
               % (cancel_at % 1000, margin_ms), path=path)
    clock.sleep_until(cancel_at, lead=False)
    t0 = time.time()
    res = wrapper.get_url(cancel_url)
    if res is None:  # one immediate retry: a late cancel beats no cancel
        res = wrapper.get_url(cancel_url)
    if res is None:
        return _finish(sid, "failed", "cancel request failed - troops will "
                       "hit the target and return on their own", path=path)

    # Report what actually happened. The cancel page renders a ms-precise
    # arrival the real event does NOT follow, so it is never trusted; the
    # response's time_generated (the server-side cancel moment) gives the
    # quantized estimate S + 2*floor(C - S), and the returning command's ms
    # clock in the command list is the authoritative value when readable.
    state = Extractor.game_state(res)
    cancel_actual = float(state["time_generated"]) if state \
        and state.get("time_generated") else (t0 * 1000 + clock.offset_ms)
    elapsed_s = max(0, int(cancel_actual - send_actual) // 1000)
    return_actual = int(send_actual + 2000 * elapsed_s)
    _, arrival_back_ms, _ = _locate_outgoing(
        wrapper, clock, village_id, snipe.get("target_x"),
        snipe.get("target_y"), return_actual)
    if arrival_back_ms is not None:
        return_actual = arrival_back_ms
    delta = return_actual - return_ms
    status = "disarmed" if disarmed else "done"
    window_note = ""
    if window_ms and not disarmed:
        window_note = " - window +%d %s" % (
            window_ms, "HIT" if 0 <= delta <= window_ms else "MISSED")
    summary = ("troops return at .%03d, %+dms vs target%s%s"
               % (return_actual % 1000, delta,
                  "" if arrival_back_ms is not None else " (estimated)",
                  window_note)) \
        if not disarmed else \
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
