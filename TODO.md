# Feature ideas / backlog

Not implemented yet, notes only, so a future session (or the user) has full
context before starting.

## Advanced bot planner: attack-aware farm/scavenge stop windows

**Problem:** an attack scheduled to launch from a village (via the Attack tab
/ `game/attack_scheduler.py`) needs its troops home. If scavenging/farming is
still sending troops out around the send time, part of the intended attack
force may be away when it needs to launch. Today `farm_enabled` /
`gather_enabled` are just per-village on/off flags with no time awareness, so
avoiding this requires manually remembering to flip them off in advance.

**Idea:** a planner that, per village, lets you set a time window (or a time
tied to a specific scheduled attack) during which farming/scavenging are
forced off, ideally computed so troops are actually *home* by the attack's
send time, not just "stopped sending new runs" at that time.

Two depths of implementation, increasing complexity/risk:
- **(A) Manual stop window**: user sets a start/stop clock per village
  (like a recurring `forced_peace_times`-style range, but for
  farm/scavenge, not just attacks). Cheap, low risk, no new state tracking.
- **(B) Auto-computed from attack arrival**: pick a village + a scheduled
  attack, and the planner back-calculates when scavenging/farming must stop
  (and possibly whether in-flight runs need to be waited out) so every troop
  is home before send_ts. Needs a live registry of outstanding
  scavenge/farm run return times, which doesn't exist today, meaningfully
  more complex and more bug-prone.

**Relevant existing code (starting points, not a plan):**
- `game/village.py:552-558`, `farm_enabled` per-village gate in `run_farming()`.
- `game/village.py:761-802` (`do_gather`), `gather_enabled` gate, plus the
  existing precedence stack: `GATHER_GROUP_POLICIES` /
  `_gather_group_policy()` (village.py:713-759) is a working precedent for a
  policy layer that overrides the per-village flag *and* the quick toggle
  (see the docstring at village.py:731: "authoritative: it beats the
  per-village gather_when_attacked flag and the quick toggle").
- `game/village.py:368-387` (`check_forced_peace`), existing recurring
  date/time-range mechanism, but it only blocks attack *sending*
  (`farms.forced_peace_times`), not farm/scavenge. Closest existing pattern
  for a "window" UI/config shape.
- `game/attack_scheduler.py`, the scheduled-attack queue
  (`cache/scheduled_attacks.json`), cross-process file-locked
  (`_Lock`/`update()`), with `next_send_ts()` already available per queue.
  Any planner tied to a specific scheduled attack should read this queue
  through the same lock discipline rather than inventing a second
  uncoordinated data source.
- `webmanager/server.py:953-992`, `QUICK_TOGGLES` / `PER_VILLAGE_TOGGLES`,
  the manual account-wide on/off switches (incl. `scavenge`/`farm`) that a
  planner window must clearly state it overrides, or users will fight their
  own quick-toggle and not understand why.

**Safety/risk notes to resolve before building:**
- No existing tracking of "which troops are out on a scavenge/farm run and
  when they return", option (B) needs this built first, and it's easy to
  get subtly wrong (miscounted troops still show as home → attack launches
  short-handed with no error).
- Precedence must be explicit and visible in the UI: a banner/alert stating
  the planner window overrides quick controls and per-village toggles for
  its duration, so it's not silently fighting a manual toggle flip.
- Decide whether this reuses/extends `forced_peace_times` or is a fully
  separate mechanism, extending touches already-relied-upon attack-timing
  code, so the blast radius of a bug is bigger.

**Open questions (ask again before starting):**
1. Manual stop-window (A) first, or go straight for auto-computed (B)?
2. Should a planner entry be 1:1 linked to a specific scheduled attack
   (auto-armed from its send_ts), or a standalone recurring daily window
   independent of any particular attack?
3. When a window starts and troops are already out (mid-run), is that a hard
   gate (attack waits / is delayed) or advisory only (stop new runs, accept
   the attack may launch a bit short)?

## Verify balancer fill_mode=even against a live world

**Status:** shipped in `3cbbfb8` and on by default; correctness confirmed by
fuzzing, but never yet observed on a running world.

**What was changed:** `_plan_even()` in `game/balancer.py` replaces
"fill the emptiest resource to the ceiling" with a water-line split, so one
send levels wood/stone/iron together instead of letting one run away from the
others. `balancer.fill_mode = "biggest_gap"` restores the old behaviour.

**Why this needs a live check:** the fuzzing covers the arithmetic, not the
setting it runs in. Three things it cannot speak to:
- Senders do not coordinate. Levelling spreads one sender's merchants over
  more resources per receiver, so each send serves a smaller slice of the
  gap. Whether that interacts badly with `max_sends_per_receiver` and the
  `may_serve()` starvation escape is an emergent question, not an arithmetic
  one - a receiver could end up flatter but slower to fill.
- Splitting three ways wastes up to three part-full merchants where a
  single-resource send wastes one. Fuzzing put this at ~386 resources per
  send on random data; the real cost depends on the merchant counts and
  warehouse sizes actually in play.
- On nl116 the old code already filled all three resources on 66% of sends
  (196-send baseline), because most receivers are small enough that the
  budget covers every gap. The change only ever had room to affect the other
  third, so the real-world win may be smaller than the fuzzing suggests.

**How to check:** the baseline and the tooling are already set up outside the
repo, see `/root/twb-balancer-tracker/README.md` - `track.py report` prints
resources-per-send and spread-change split by old vs new code. Delete that
directory once this item is closed.

**Open question:** if levelling does prove slower to fill a village, the fix
is probably to let a receiver be served more than once per window rather than
to go back to `biggest_gap` - worth deciding deliberately rather than by
flipping the setting back.

## Make the report "reset to Alle" optional, if captchas prove request-driven

**Status:** blocked on measurement. The tracker is running; do not act on this
until it has a verdict.

**What is already shipped:** `reset_group_view()` in `game/reports.py` puts the
report screen back on the main folder after the bot has walked the filtered
ones, so the player is not left staring at Farm-assistent every time they open
the game. It costs one extra GET per village cycle - about 37 per pass, ~4% of
everything the bot does.

**Why that might matter:** bot protection on nl116 fires roughly 3.8 times a
day, once per ~6,000 requests, and each one stalls the bot until the captcha is
solved in a browser (31 hours of stall over the four days to 2026-08-27, the
worst of it overnight). Nothing about *which* endpoint or *what time of day*
predicts a hit, and neither elapsed time nor request count showed a threshold -
which points at a random per-request check. If that is right, every avoidable
request is a proportional share of the captchas, and a purely cosmetic 4% is
worth being able to switch off.

**Measure first:** `/root/twb-captcha-tracker/track.py report`. Section 1 is
the whole point - it buckets the bot's own running time by request rate and
asks whether captchas per hour or captchas per 1000 requests is the flatter
number. Needs ~12 captchas recorded after the instrumentation restart, so
roughly 3 days. The instrumentation itself is the counter in
`core/request.py` (`_track_request` / `track_event`); it changes no behaviour
and makes no extra requests.

**Then:**
- **Flatter per 1000 requests** -> requests are the currency. Add
  `reports.reset_group_view` (default true, since it exists to fix a real
  annoyance) so it can be turned off, and take the much bigger win first:
  report polling is account-wide but re-walked by all 37 villages every cycle,
  4 list GETs each, ~17% of all requests with three quarters of it redundant.
  A short account-wide TTL would cut ~15%. The trade-off there is farm
  decisions (`has_resources_left` / `safe_to_engage` in `game/attack.py` and
  `game/barbshaper.py`) acting on slightly staler reports.
- **Flatter per hour** -> requests are not the currency. Leave the reset alone,
  drop the TTL idea entirely, and note in this file that request-count
  optimisation is a dead end for captchas so nobody re-derives it.

Delete `/root/twb-captcha-tracker/` and back out the `core/request.py`
instrumentation once this is closed.
