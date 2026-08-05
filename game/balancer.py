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
"""
import logging
import math
import re
import time
from html import unescape

from core.filemanager import FileManager

RESOURCES = ("wood", "stone", "iron")
MERCHANT_CAPACITY = 1000
STATE_FILE = "cache/balancer.json"


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
        self.target_order = "nearest"
        self.sender_order = "nearest"
        self.send_cooldown = 3600
        self.max_sends_per_receiver = 1
        self.reserve_merchants = 0
        self.min_send_amount = 250
        self.sender_keep = 0

    # ---------------------------------------------------------------- helpers

    def _cooldown_left(self):
        last = _load_state().get(self.village_id, {}).get("last_send", 0)
        return max(0, int(last + self.send_cooldown - time.time()))

    def _mark_sent(self, target_id, amounts):
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

    def _fill_ratio(self, village):
        """How full the village is, 0..1, averaged over the three resources."""
        cap = int(village.get("storage_max") or 0)
        if cap <= 0:
            return 1.0
        res = village.get("resources") or {}
        total = sum(min(int(res.get(r, 0) or 0), cap) for r in RESOURCES)
        return total / float(cap * len(RESOURCES))

    def _headroom(self, village):
        """Per-resource room left below target_fill_pct of the receiver's cap."""
        cap = int(village.get("storage_max") or 0)
        if cap <= 0:
            return {}
        ceiling = int(cap * self.target_fill_pct / 100.0)
        res = village.get("resources") or {}
        room = {}
        for r in RESOURCES:
            have = int(res.get(r, 0) or 0)
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

        candidates = []
        for vid, village in villages.items():
            if vid == self.village_id:
                continue
            points = self._points(village)
            if points > self.receiver_max_points or points >= my_points:
                continue
            room = self._headroom(village)
            if not any(v >= self.min_send_amount for v in room.values()):
                continue
            candidates.append((vid, village, room))

        if self.target_order == "emptiest":
            candidates.sort(key=lambda c: self._fill_ratio(c[1]))
        else:
            candidates.sort(
                key=lambda c: self._distance(my_loc, self._location(c[1])))
        return candidates

    def _spare_stock(self, village):
        """Total resources this village could give away."""
        res = village.get("resources") or {}
        return sum(max(0, int(res.get(r, 0) or 0) - self.sender_keep)
                   for r in RESOURCES)

    def rank_senders(self, villages, target):
        """Qualifying senders for one target, best first.

        'nearest' is relative to the target, so the ranking is computed per
        target rather than once globally.
        """
        target_loc = self._location(target)
        senders = [
            (vid, v) for vid, v in villages.items()
            if self._points(v) >= self.sender_min_points
            and self._points(v) > self._points(target)
        ]
        if self.sender_order == "most_resources":
            senders.sort(key=lambda s: -self._spare_stock(s[1]))
        elif self.sender_order == "highest_points":
            senders.sort(key=lambda s: -self._points(s[1]))
        else:
            senders.sort(
                key=lambda s: self._distance(target_loc, self._location(s[1])))
        return [vid for vid, _ in senders]

    def may_serve(self, target_id, ranked):
        """Whether this village should be the one to feed `target_id`.

        The preferred sender always may. A lower-ranked one only steps in once
        the target has gone a full cooldown without a delivery - otherwise a
        target whose designated sender is out of merchants (the normal state for
        a heavy trading village) would never be fed at all.
        """
        if not ranked or ranked[0] == self.village_id:
            return True
        if self.village_id not in ranked:
            return False
        # Measure from the last delivery; only fall back to when the target was
        # first seen as needy if it has never been served at all.
        last = self._last_delivery(target_id)
        since = time.time() - (last or self._first_seen(target_id))
        if since >= self.send_cooldown:
            self.logger.debug(
                "Stepping in for %s: preferred sender %s has not delivered in %ds",
                target_id, ranked[0], int(since))
            return True
        return False

    # ------------------------------------------------------------------ market

    def merchants_available(self, page_text):
        found = re.search(r'market_merchant_available_count">(\d+)', page_text)
        return int(found.group(1)) if found else 0

    def _plan(self, room, stock, merchants):
        """Split the free merchants over the resources the target has room for.

        Merchants are the scarce thing and they are *discrete*: one carries 1000
        of a single resource, and a 3056 send occupies four of them, not 3.056.
        Budgeting in raw resources instead of whole merchants overcommits, so
        every allocation here is counted in merchants and only converted back to
        an amount at the end. Capped by merchants, then the sender's own stock,
        then the receiver's headroom.
        """
        plan = {}
        left = int(merchants)
        # Biggest need first: a merchant is worth most where the gap is widest.
        for res, want in sorted(room.items(), key=lambda kv: -kv[1]):
            if left < 1:
                break
            spare = max(0, int(stock.get(res, 0) or 0) - self.sender_keep)
            amount = min(int(want), spare, left * MERCHANT_CAPACITY)
            if amount < self.min_send_amount:
                continue
            plan[res] = amount
            left -= -(-amount // MERCHANT_CAPACITY)  # ceil
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
        """
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

    def run(self, my_points, my_stock, has_own_needs=False):
        """Send spare resources to poorer villages. Returns sends performed."""
        if not self.enabled:
            return 0
        if my_points < self.sender_min_points:
            self.logger.debug(
                "Not a sender: %d points is under the %d threshold",
                my_points, self.sender_min_points)
            return 0
        if has_own_needs:
            self.logger.debug(
                "Not sending: this village still needs resources itself")
            return 0
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
            self._mark_sent(vid, plan)
            sent += 1
        return sent
