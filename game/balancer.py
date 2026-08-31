"""
Village-to-village resource balancing.

Rich villages (by points) push resources to poor ones so a freshly conquered or
rim village can grow without waiting on its own tiny production. This is the
in-game "send resources" screen, not the market's trade offers - no ratio, no
counter-party, just merchants moving goods between your own villages.

Target selection is driven by how full the *receiver* is, never by what the
sender can spare: a rim village at storage 4 holds 2.8k per resource, so an
uncapped send would overflow and waste merchant trips. Amounts are always
clamped to `target_fill_pct` of the receiver's own warehouse.

"How full the receiver is" is read from the receiver's own snapshot, which it
writes when it runs - up to a full bot pass ago. A delivery that has landed
since then is invisible, and so is one still on the road, so several senders
in a row each plan against the same pre-delivery stock and fill the same empty
warehouse again: that is how a 62k warehouse ends up with 86k of wood on its
way. So every send is written down together with the moment it lands, and
`_headroom` counts anything that lands after the snapshot was taken as already
delivered. See `_mark_sent` and `_pending`.
"""
import logging
import math
import re
import time
from html import unescape

from core.filemanager import FileManager

RESOURCES = ("wood", "stone", "iron")
MERCHANT_CAPACITY = 1000
# Last-resort guess at the market's speed, used only until the world has been
# measured: the classic TW market runs at 30 minutes per field divided by the
# world speed. Do not trust it - nl116 has speed 1.5, so this predicts 20
# minutes per field and the game's own confirmation page says 1. Every send
# reads the real trip time off that page and stores it (see
# _record_merchant_speed), so this only ever decides sends made before the
# first confirmation page has been read.
MERCHANT_MINUTES_PER_FIELD = 30
# Ceiling on a computed trip, so an unknown coordinate cannot block a receiver
# for good. See ResourceBalancer.travel_seconds.
MAX_TRAVEL_SECONDS = 24 * 3600
STATE_FILE = "cache/balancer.json"
WORLD_CONFIG_FILE = "cache/world/config.json"


def _load_state():
    if not FileManager.path_exists(STATE_FILE):
        return {}
    return FileManager.load_json_file(STATE_FILE) or {}


def _save_state(state):
    FileManager.save_json_file_atomic(state, STATE_FILE)


class ResourceBalancer:
    """Pushes spare resources from one village to poorer ones."""

    def __init__(self, wrapper, village_id):
        self.wrapper = wrapper
        self.village_id = str(village_id)
        self.logger = logging.getLogger("Balancer: %s" % village_id)

        self.enabled = False
        self.sender_min_points = 4000
        self.receiver_max_points = 1000
        self.target_fill_pct = 90
        self.fill_mode = "even"
        self.target_order = "nearest"
        self.sender_order = "nearest"
        self.send_cooldown = 3600
        self.max_sends_per_receiver = 1
        self.reserve_merchants = 0
        self.min_send_amount = 250
        self.sender_keep = 0
        # Minutes per field to assume until the game has told us better. None
        # means "no idea", and falls back to the classic-TW guess.
        self.merchant_minutes_per_field = None
        self._world_speed_cache = None
        # Trip time the game stated for the last send, filled in by send().
        self.last_travel_seconds = None
        # Per-resource hold-back for this village's own unpaid queue, filled in
        # by run() from the resource manager. See _needed_resources().
        self.reserve = {}

    # ---------------------------------------------------------------- helpers

    def _cooldown_left(self):
        last = _load_state().get(self.village_id, {}).get("last_send", 0)
        return max(0, int(last + self.send_cooldown - time.time()))

    def _mark_sent(self, target_id, amounts, arrival):
        state = _load_state()
        state[self.village_id] = {
            "last_send": int(time.time()),
            "last_target": str(target_id),
            "last_amounts": amounts,
        }
        # Deliveries are tracked per receiver, so any sender can tell whether the
        # village it wants to help has already been served and how often.
        deliveries = state.setdefault("_deliveries", {})
        key = str(target_id)
        now = int(time.time())
        # Only the current window is ever read, so keep a day of history and let
        # the rest go - otherwise this list grows for the life of the world.
        history = [t for t in self._delivery_times(target_id) if t >= now - 86400]
        deliveries[key] = history + [now]
        # The same send, kept a second time with its arrival: until it lands,
        # nobody may count the receiver's warehouse as having room for it.
        flights = state.setdefault("_inflight", {})
        pending = [f for f in (flights.get(key) or [])
                   if int(f.get("arrival") or 0) >= now - 86400]
        pending.append({
            "sent": now,
            "arrival": int(arrival),
            "amounts": {r: int(v) for r, v in amounts.items() if r in RESOURCES},
        })
        flights[key] = pending
        _save_state(state)

    @staticmethod
    def _delivery_times(target_id):
        """Timestamps of deliveries to this receiver, oldest first.

        Older state stored a single timestamp per receiver; read it as a
        one-element history so upgrading does not lose the record.
        """
        raw = (_load_state().get("_deliveries") or {}).get(str(target_id))
        if raw is None:
            return []
        if isinstance(raw, (int, float)):
            return [int(raw)]
        return [int(t) for t in raw]

    def _last_delivery(self, target_id):
        times = self._delivery_times(target_id)
        return max(times) if times else 0

    def deliveries_in_window(self, target_id):
        """How many times this receiver has been served within one cooldown."""
        cutoff = time.time() - self.send_cooldown
        return len([t for t in self._delivery_times(target_id) if t >= cutoff])

    @staticmethod
    def _first_seen(target_id):
        """When this target first came up as needing resources.

        Without it, a target with no delivery history looks infinitely starved,
        so every sender's escape hatch fires on the same pass and they all ship
        to the same village against the same stale headroom. Stamping the first
        sighting gives the preferred sender one cooldown of exclusivity before
        anyone else is allowed to step in.
        """
        state = _load_state()
        seen = state.setdefault("_seen", {})
        key = str(target_id)
        if key not in seen:
            seen[key] = int(time.time())
            _save_state(state)
        return int(seen[key])

    def _mark_turn(self):
        """Record that this village got its chance to send during this pass."""
        state = _load_state()
        turns = state.setdefault("_turns", {})
        turns[self.village_id] = int(time.time())
        _save_state(state)

    @staticmethod
    def _last_turn(village_id):
        """When that village last reached the sending stage of its own run."""
        return int((_load_state().get("_turns") or {}).get(str(village_id), 0))

    @staticmethod
    def _managed_villages():
        """Every managed village's last snapshot, keyed by id.

        These are written by each village at the end of its own run, so they are
        up to one full bot pass old. That is fine for points and coordinates,
        and close enough for stock levels: between passes a small village
        produces far less than the headroom being filled.
        """
        out = {}
        for name in FileManager.list_directory("cache/managed", ends_with=".json"):
            data = FileManager.load_json_file("cache/managed/%s" % name)
            if data:
                out[name.replace(".json", "")] = data
        return out

    @staticmethod
    def _points(village):
        return int(((village or {}).get("public") or {}).get("points") or 0)

    @staticmethod
    def _location(village):
        loc = ((village or {}).get("public") or {}).get("location")
        if isinstance(loc, (list, tuple)) and len(loc) == 2:
            try:
                return int(loc[0]), int(loc[1])
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _distance(a, b):
        if not a or not b:
            return 9999.0
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _world_speed(self):
        if self._world_speed_cache is None:
            config = FileManager.load_json_file(WORLD_CONFIG_FILE) or {}
            try:
                speed = float(config.get("speed") or 1.0)
            except (TypeError, ValueError):
                speed = 1.0
            self._world_speed_cache = speed if speed > 0 else 1.0
        return self._world_speed_cache

    def minutes_per_field(self):
        """How fast this world's merchants are.

        Three sources, best first: what the game timed for one of our own
        sends, what the config says to assume, and the classic-TW guess. The
        measurement always wins - it comes from the confirmation page of a real
        send, so it is not an estimate at all - which means the config setting
        only matters on a world that has not sent anything yet.
        """
        measured = (_load_state().get("_merchant") or {}).get("minutes_per_field")
        for candidate in (measured, self.merchant_minutes_per_field):
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return MERCHANT_MINUTES_PER_FIELD / self._world_speed()

    def _record_merchant_speed(self, distance, seconds):
        """Remember a trip the game itself timed for us.

        One send tells us everything: the confirmation page prints the one-way
        duration, and we know how far it was going. Storing the rate rather
        than the trip makes it usable for every other pair of villages.
        """
        if distance <= 0 or seconds <= 0:
            return
        rate = round(seconds / 60.0 / distance, 4)
        state = _load_state()
        known = (state.get("_merchant") or {}).get("minutes_per_field")
        state["_merchant"] = {
            "minutes_per_field": rate,
            "measured": int(time.time()),
            "over_fields": round(distance, 3),
            "seconds": int(seconds),
        }
        _save_state(state)
        if known != rate:
            self.logger.info(
                "Merchants on this world travel %.3f min/field "
                "(timed over %.1f fields by the game itself)", rate, distance)

    def travel_seconds(self, sender, receiver):
        """How long merchants need between two villages, rounded up.

        Only used when the game has not told us directly - the first send on a
        world, or a confirmation page this cannot read. Rounding up is
        deliberate: overestimating the trip makes this village wait a little
        longer before topping the receiver up again, while underestimating it
        declares a convoy landed while it is still on the road, which is the
        exact overshoot the ledger exists to prevent.

        A missing coordinate makes `_distance` return its 9999 sentinel, which
        would park the receiver behind a convoy arriving in a hundred years,
        so the result is capped at a day - longer than any trip between two
        villages of the same account.
        """
        distance = self._distance(self._location(sender), self._location(receiver))
        return min(int(math.ceil(distance * self.minutes_per_field() * 60)),
                   MAX_TRAVEL_SECONDS)

    @staticmethod
    def _pending(flights, since):
        """Resources on the road that a snapshot taken at `since` cannot show.

        A village's snapshot records what it held when it last ran, which
        means it already includes every convoy that had landed by then and no
        others. So a send still counts as outstanding exactly while its
        arrival is later than that moment - one test that covers both a convoy
        genuinely still travelling and one that landed after the receiver last
        wrote down what it was holding.

        A receiver that has never written a `last_run` (an older snapshot)
        gives `since` of 0, so everything sent recently is counted. That is the
        safe way round: it under-fills rather than overflows.
        """
        total = {}
        for flight in flights or []:
            if int(flight.get("arrival") or 0) <= since:
                continue
            for res, amount in (flight.get("amounts") or {}).items():
                if res in RESOURCES:
                    total[res] = total.get(res, 0) + int(amount)
        return total

    @staticmethod
    def _in_flight_table():
        """Every receiver's outstanding convoys, read once per pass."""
        return _load_state().get("_inflight") or {}

    def _fill_ratio(self, village, coming=None):
        """How full the village is, 0..1, averaged over the three resources.

        Counts inbound convoys as already delivered, so `target_order`
        "emptiest" stops ranking a village that has three loads on the way
        ahead of one that has none.
        """
        cap = int(village.get("storage_max") or 0)
        if cap <= 0:
            return 1.0
        res = village.get("resources") or {}
        coming = coming or {}
        total = sum(min(int(res.get(r, 0) or 0) + coming.get(r, 0), cap)
                    for r in RESOURCES)
        return total / float(cap * len(RESOURCES))

    def _headroom(self, village, coming=None):
        """Per-resource room left below target_fill_pct of the receiver's cap.

        Resources still walking towards the village count as if they had
        arrived. Without that every sender plans against the same
        pre-delivery stock, and three of them independently fill the same
        empty warehouse - which is how a 62k warehouse gets 86k of wood.
        """
        cap = int(village.get("storage_max") or 0)
        if cap <= 0:
            return {}
        ceiling = int(cap * self.target_fill_pct / 100.0)
        res = village.get("resources") or {}
        coming = coming or {}
        room = {}
        for r in RESOURCES:
            have = int(res.get(r, 0) or 0) + coming.get(r, 0)
            if ceiling > have:
                room[r] = ceiling - have
        return room

    # ----------------------------------------------------------------- picking

    def pick_targets(self, villages, my_points):
        """Villages under the receiver threshold that still have room, ordered
        by the configured preference. The sender never targets itself, and a
        village only counts as a receiver if it is genuinely poorer."""
        me = villages.get(self.village_id) or {}
        my_loc = self._location(me)
        # One read of the ledger for the whole pass; every candidate below is
        # judged on stock plus whatever is still walking towards it.
        flights = self._in_flight_table()

        candidates = []
        for vid, village in villages.items():
            if vid == self.village_id:
                continue
            points = self._points(village)
            if points > self.receiver_max_points or points >= my_points:
                continue
            coming = self._pending(flights.get(vid),
                                   int(village.get("last_run") or 0))
            room = self._headroom(village, coming)
            if not any(v >= self.min_send_amount for v in room.values()):
                if coming:
                    self.logger.debug(
                        "Skipping %s: %s already on the way covers its room",
                        vid, ", ".join("%d %s" % (v, k)
                                       for k, v in coming.items()))
                continue
            candidates.append((vid, village, room, coming))

        if self.target_order == "emptiest":
            candidates.sort(key=lambda c: self._fill_ratio(c[1], c[3]))
        else:
            candidates.sort(
                key=lambda c: self._distance(my_loc, self._location(c[1])))
        return [(vid, village, room) for vid, village, room, _ in candidates]

    @staticmethod
    def _needed_resources(requested):
        """What a village still cannot pay for, per resource.

        `requested` is the resource manager's {source: {resource: shortfall}}:
        an entry appears only when the village wants something it cannot
        currently afford, and is zeroed again as soon as it can.

        Two things are deliberately dropped. `pop` is not a resource - it is
        missing farm space, and no amount of wood fixes it, so a village that
        wants a farm it has no room for must not be stopped from giving its
        overflowing warehouse away. And sources are combined with max() rather
        than sum(): each one computes its shortfall against the same stock, so
        adding them up counts the same missing wood once per queue item.
        """
        need = {}
        for wants in (requested or {}).values():
            for res, amount in (wants or {}).items():
                if res in RESOURCES and amount and int(amount) > 0:
                    need[res] = max(need.get(res, 0), int(amount))
        return need

    def _spare(self, res, stock):
        """How much of one resource this village may give away.

        Held back: the flat `sender_keep`, plus whatever its own build or
        recruit queue still cannot pay for. Reserving the shortfall alone, and
        only of the resource that is actually short, is the whole difference
        between a queue item costing the sender 5k wood and it costing every
        send the village would otherwise have made.
        """
        have = int(stock.get(res, 0) or 0)
        return max(0, have - self.sender_keep - int(self.reserve.get(res, 0)))

    def _spare_stock(self, village):
        """Total resources this village could give away.

        Reads another village's snapshot, so the queue shortfall comes from the
        `required_resources` it wrote there rather than from a live manager.
        """
        res = village.get("resources") or {}
        need = self._needed_resources(village.get("required_resources"))
        return sum(max(0, int(res.get(r, 0) or 0) - self.sender_keep - need.get(r, 0))
                   for r in RESOURCES)

    def _spare_fill(self, village):
        """Spare stock as a fraction of this village's own warehouse, 0..1.

        Ranking on raw stock always hands the job to whoever built the biggest
        warehouse, even when it sits half empty while a smaller village is
        about to overflow. Measuring each sender against its own capacity picks
        the one closest to wasting production instead.
        """
        cap = int(village.get("storage_max") or 0)
        if cap <= 0:
            return 0.0
        return self._spare_stock(village) / float(cap * len(RESOURCES))

    def rank_senders(self, villages, target):
        """Qualifying senders for one target, best first.

        'nearest' is relative to the target, so the ranking is computed per
        target rather than once globally.

        Every sender must rank a given target identically - that agreement is
        what lets `may_serve` decide who owns it without any of them talking to
        each other - so ties break on village id rather than on whatever order
        the snapshots happened to be read off disk in.
        """
        target_loc = self._location(target)
        senders = [
            (vid, v) for vid, v in villages.items()
            if self._points(v) >= self.sender_min_points
            and self._points(v) > self._points(target)
        ]
        if self.sender_order == "most_resources":
            senders.sort(key=lambda s: (-self._spare_stock(s[1]), s[0]))
        elif self.sender_order == "fullest":
            senders.sort(key=lambda s: (-self._spare_fill(s[1]), s[0]))
        elif self.sender_order == "highest_points":
            senders.sort(key=lambda s: (-self._points(s[1]), s[0]))
        else:
            senders.sort(
                key=lambda s: (self._distance(target_loc, self._location(s[1])),
                               s[0]))
        return [vid for vid, _ in senders]

    def may_serve(self, target_id, ranked):
        """Whether this village should be the one to feed `target_id`.

        The preferred sender always may. A lower-ranked one steps in only once
        the preferred sender has demonstrably passed on the job - otherwise a
        target whose designated sender is out of merchants (the normal state for
        a heavy trading village) would never be fed at all.

        "Passed on the job" used to mean "the target has gone a full cooldown
        without a delivery", and that opens the door for every sender at the
        same instant the target becomes eligible again. Whichever village the
        bot happens to visit first that pass claims it, `max_sends_per_receiver`
        then locks everyone else out, and the configured order never gets a
        say: the village at the top of the run order feeds the whole rim.

        Measuring against the preferred sender's own last turn removes the
        race. It has to have actually run, and left this target unserved,
        before anyone else may take it.
        """
        if not ranked or ranked[0] == self.village_id:
            return True
        if self.village_id not in ranked:
            return False
        preferred = ranked[0]
        turn = self._last_turn(preferred)
        opened = self._eligible_since(target_id)
        # The preferred sender has run since this receiver came back up for a
        # delivery, and still sent it nothing: no merchants, or nothing to
        # spare. Comparing against when the *window* opened rather than
        # against the last delivery matters - a turn taken while the receiver
        # was still in its cooldown is not a turn it passed on.
        if turn > opened:
            self.logger.debug(
                "Stepping in for %s: preferred sender %s had its turn and passed",
                target_id, preferred)
            return True
        # Backstop for a preferred sender that is not running at all - never
        # has, or not since before the window opened a whole cooldown ago.
        # Without it a paused or balancer-disabled village would still rank
        # first and quietly starve everything it was picked for.
        idle = time.time() - max(turn, opened)
        if idle >= self.send_cooldown:
            self.logger.debug(
                "Stepping in for %s: preferred sender %s has not run in %ds",
                target_id, preferred, int(idle))
            return True
        return False

    def _eligible_since(self, target_id):
        """When this receiver last became free to accept another delivery.

        `max_sends_per_receiver` deliveries inside one cooldown fill the
        window, so it reopens when the oldest of those ages out. A receiver
        that has never had a full window has been eligible since it was first
        seen as needy.
        """
        cap = max(1, self.max_sends_per_receiver)
        times = sorted(self._delivery_times(target_id))
        if len(times) >= cap:
            return times[-cap] + self.send_cooldown
        return self._first_seen(target_id)

    # ------------------------------------------------------------------ market

    @staticmethod
    def parse_travel_seconds(page_text):
        """The one-way trip time the confirmation page itself states.

        The page lists the duration, then the arrival, then the return - but
        under translated labels, so matching on the words would only work in
        one language. The times are read as a group instead and made to prove
        themselves: the return is exactly one duration after the arrival, so
        the gap between the last two clock times has to appear again in the
        list as the stated duration. Anything that fails to line up (a page
        with an unexpected clock in it, a layout change) returns None, and the
        caller falls back to the distance model.
        """
        times = [int(h) * 3600 + int(m) * 60 + int(sec)
                 for h, m, sec in re.findall(
                     r"\b(\d{1,3}):([0-5]\d):([0-5]\d)\b", page_text)]
        if len(times) < 3:
            return None
        gap = (times[-1] - times[-2]) % 86400
        if not 0 < gap <= MAX_TRAVEL_SECONDS:
            return None
        if gap not in times[:-2]:
            return None
        return gap

    def merchants_available(self, page_text):
        found = re.search(r'market_merchant_available_count">(\d+)', page_text)
        return int(found.group(1)) if found else 0

    def _plan(self, room, stock, merchants):
        """Split the free merchants over the resources the target has room for.

        Merchants are the scarce thing and they are *discrete*: one carries 1000
        of a single resource, and a 3056 send occupies four of them, not 3.056.
        Budgeting in raw resources instead of whole merchants overcommits, so
        both modes below settle up in whole merchants before returning. Every
        plan is capped by merchants, by the sender's own stock, and by the
        receiver's headroom.
        """
        if self.fill_mode == "even":
            return self._plan_even(room, stock, merchants)
        return self._plan_biggest_gap(room, stock, merchants)

    def _plan_biggest_gap(self, room, stock, merchants):
        """Fill the emptiest resource to the ceiling, then the next one.

        Cheap and fine while the budget is small next to the gaps, but a big
        budget lets the first resource leapfrog the others in a single send.
        """
        plan = {}
        left = int(merchants)
        # Biggest need first: a merchant is worth most where the gap is widest.
        for res, want in sorted(room.items(), key=lambda kv: -kv[1]):
            if left < 1:
                break
            amount = min(int(want), self._spare(res, stock),
                         left * MERCHANT_CAPACITY)
            if amount < self.min_send_amount:
                continue
            plan[res] = amount
            left -= -(-amount // MERCHANT_CAPACITY)  # ceil
        return plan

    def _plan_even(self, room, stock, merchants):
        """Level the receiver's three resources instead of topping one up.

        Every resource shares one ceiling, so the one with the most room is the
        one the receiver has least of. Pouring the whole budget into it - what
        biggest-gap-first does - can push it past the others in a single send:
        1334 wood next to 16274 iron becomes 18334 wood next to 3949 stone.

        So the budget floods in from the bottom instead, like water finding its
        level: the emptiest resource rises to meet the second emptiest, then
        both rise together, until the merchants run out. The receiver ends up
        as flat as the budget allows; the sender gives up uneven amounts, which
        is the cheaper problem to have in a developed village.
        """
        # How far each resource *could* be raised. The sender's stock only caps
        # that distance - it must never decide the ordering. Folding stock in
        # any earlier ranks the resources by what the sender happens to hold
        # and levels the wrong ones: a receiver on 9809 iron and 97061 stone,
        # fed by a sender sitting on stone, gets its fullest resource topped up.
        cap = {}
        for res, want in room.items():
            limit = min(int(want), self._spare(res, stock))
            if limit > 0:
                cap[res] = limit
        budget = int(merchants) * MERCHANT_CAPACITY
        if not cap or budget <= 0:
            return {}

        def fill_to(line):
            """Raise everything that sits deeper than `line` up to it."""
            return {r: min(max(0, int(room[r]) - line), cap[r]) for r in cap}

        # The water line is a distance below the ceiling, shared by all three.
        # A high line costs nothing, a line of 0 fills every resource to the
        # ceiling; the lowest line the budget can pay for is the flattest
        # outcome available. Resources the sender cannot supply drop out at
        # their cap on their own, and their share of the budget goes to the
        # rest, which is exactly what should happen.
        low, high = 0, max(int(room[r]) for r in cap)
        while low < high:
            mid = (low + high) // 2
            if sum(fill_to(mid).values()) > budget:
                low = mid + 1
            else:
                high = mid

        # A zero allocation is not a send; min_send_amount may itself be 0.
        plan = {r: a for r, a in fill_to(low).items()
                if a > 0 and a >= self.min_send_amount}
        # Budgeting in resources rather than merchants can round up to one
        # merchant per resource over once the per-resource ceil is applied.
        # Shave the biggest allocation down to a whole merchant until it fits.
        while plan and self.merchants_for(plan) > merchants:
            res = max(plan, key=lambda r: plan[r])
            plan[res] -= plan[res] % MERCHANT_CAPACITY or MERCHANT_CAPACITY
            if plan[res] <= 0 or plan[res] < self.min_send_amount:
                del plan[res]
        return plan

    @staticmethod
    def merchants_for(plan):
        """Whole merchants a plan occupies."""
        return sum(-(-v // MERCHANT_CAPACITY) for v in plan.values())

    def send(self, target, amounts):
        """Two-step in-game send: fill the form, then confirm it.

        The confirm page echoes hidden fields (a per-send token among them);
        posting them straight back keeps this working without hard-coding names
        the game may change. Worlds that set confirmation_skipping_hash still
        work - the first response is then already the result page.

        The confirmation page also states how long the merchants will be on the
        road. That is the one number the bot cannot work out for itself, so it
        is read off into `last_travel_seconds` on the way past; run() turns it
        into the delivery's arrival time.
        """
        self.last_travel_seconds = None
        loc = self._location(target)
        if not loc:
            self.logger.warning("Target has no coordinates, skipping")
            return False

        payload = {
            "x": loc[0],
            "y": loc[1],
            "target_type": "coord",
            "input": "%d|%d" % loc,
            "h": self.wrapper.last_h,
        }
        for res in RESOURCES:
            payload[res] = amounts.get(res, "") or ""

        url = ("game.php?village=%s&screen=market&mode=send&try=confirm_send"
               % self.village_id)
        result = self.wrapper.post_url(url, data=payload)
        if not result:
            return False

        # The confirmation form carries its own action URL, which is NOT the one
        # the first step was posted to: it is screen=market&action=send with a
        # fresh h token in the query string. Posting the confirmation anywhere
        # else silently re-renders the send form and dispatches nothing, so the
        # action is always read back off the page rather than reconstructed.
        form = re.search(r'<form[^>]*id="market-confirm-form"[^>]*>', result.text)
        if not form:
            self.logger.warning(
                "No confirmation form returned - nothing was sent to %s",
                amounts and target.get("name"))
            return False
        action = re.search(r'action="([^"]+)"', form.group(0))
        if not action:
            self.logger.warning("Confirmation form has no action, nothing sent")
            return False
        confirm_url = unescape(action.group(1))

        self.last_travel_seconds = self.parse_travel_seconds(result.text)

        hidden = dict(re.findall(
            r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
            result.text))
        if not hidden:
            self.logger.warning("Confirmation form has no fields, nothing sent")
            return False

        confirmed = self.wrapper.post_url(confirm_url, data=hidden)
        if not confirmed:
            return False
        # Still sitting on the confirmation page means the send did not go
        # through; never report a phantom delivery.
        if 'id="market-confirm-form"' in confirmed.text:
            self.logger.warning(
                "Send was not confirmed by the game, treating as failed")
            return False
        return True

    # -------------------------------------------------------------------- main

    def run(self, my_points, my_stock, my_needs=None):
        """Send spare resources to poorer villages. Returns sends performed.

        `my_needs` is the resource manager's request table. It used to be a
        single boolean that vetoed the whole run - one unpaid queue item and a
        village with three full warehouses sent nothing to anybody, including
        the two resources it was drowning in. It is now a per-resource
        hold-back instead: the queue keeps first claim on what it is short of,
        and everything above that still goes out.
        """
        if not self.enabled:
            return 0
        if my_points < self.sender_min_points:
            self.logger.debug(
                "Not a sender: %d points is under the %d threshold",
                my_points, self.sender_min_points)
            return 0
        # From here on this village counts as having had its turn this pass,
        # whether or not it ends up sending anything. Everything below is a
        # reason it *cannot* send - its own cooldown, no free merchants,
        # nothing spare - and each of those is a reason a lower-ranked sender
        # should be allowed to cover for it.
        self._mark_turn()
        self.reserve = self._needed_resources(my_needs)
        if self.reserve:
            self.logger.debug(
                "Holding back for this village's own queue: %s",
                ", ".join("%d %s" % (v, k) for k, v in self.reserve.items()))
        left = self._cooldown_left()
        if left:
            self.logger.debug("Not sending for another %d seconds", left)
            return 0

        villages = self._managed_villages()
        targets = self.pick_targets(villages, my_points)
        if not targets:
            self.logger.debug("No village under %d points has room right now",
                              self.receiver_max_points)
            return 0

        url = "game.php?village=%s&screen=market&mode=send" % self.village_id
        page = self.wrapper.get_url(url)
        if not page:
            return 0
        merchants = self.merchants_available(page.text) - self.reserve_merchants
        if merchants < 1:
            self.logger.info(
                "No merchants free to balance resources (%d reserved)",
                self.reserve_merchants)
            return 0

        sent = 0
        # Every eligible receiver is considered; the cap is per receiver, not
        # per sender, so one sender can top up several villages in a pass while
        # no village gets served twice over.
        for vid, target, room in targets:
            if merchants < 1:
                break
            served = self.deliveries_in_window(vid)
            if served >= self.max_sends_per_receiver:
                self.logger.debug(
                    "Skipping %s: already had %d delivery/deliveries this window",
                    vid, served)
                continue
            if not self.may_serve(vid, self.rank_senders(villages, target)):
                self.logger.debug(
                    "Leaving %s to a better-ranked sender", vid)
                continue
            plan = self._plan(room, my_stock, merchants)
            if not plan:
                continue
            used = self.merchants_for(plan)
            name = target.get("name") or vid
            self.logger.info(
                "Sending %s to %s (%d merchants)",
                ", ".join("%d %s" % (v, k) for k, v in plan.items()), name, used)
            if not self.send(target, plan):
                continue
            self.wrapper.reporter.report(
                self.village_id, "TWB_BALANCE",
                "Sent %s to %s" % (
                    ", ".join("%d %s" % (v, k) for k, v in plan.items()), name))
            for res, amount in plan.items():
                my_stock[res] = max(0, int(my_stock.get(res, 0)) - amount)
            merchants -= used
            me = villages.get(self.village_id)
            here, there = self._location(me), self._location(target)
            travel = self.last_travel_seconds
            if travel and here and there:
                # The game timed this trip, and both ends are known, so it also
                # measures this world's merchants for every later send. Both
                # coordinates have to be real: `_distance` answers 9999 for a
                # missing one, which would bank an absurdly fast rate and make
                # every future convoy look like it had already landed.
                self._record_merchant_speed(self._distance(here, there), travel)
            elif not travel:
                travel = self.travel_seconds(me, target)
            self._mark_sent(vid, plan, time.time() + travel)
            sent += 1
        return sent
