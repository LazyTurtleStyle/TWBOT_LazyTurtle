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
