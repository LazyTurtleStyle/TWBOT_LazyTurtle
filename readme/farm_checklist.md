# Farm setup checklist

Run through this before turning on `farms.farm`. The bot will not warn you about most of
these - it just won't farm, or it'll farm with the wrong assumptions.

- [ ] **In-game Farm Assistant templates exist**: at least an A (scout) and a B (minimal)
  template, created in-game under Farm Assistant settings.
- [ ] **`farms.template_id_scout`** set to the A template's id (click its send button once
  in-game and read the `template_id` from the network request).
- [ ] **`farms.template_id_minimal`** set to the B template's id, the same way.
- [ ] **`farms.template_minimal_troops`** set to exactly what the B template sends, e.g.
  `{"light": 5, "spy": 1}`. There is no API to read this back - if it's wrong the wall-risk
  check below silently does nothing.
- [ ] **Farm army is light cavalry only.** The wall-risk check for the C farm
  (`farm_from_report`, see `game/attack.py:report_farm_too_risky`) assumes the farm army is
  pure light cavalry and estimates troop count from carry capacity. If you farm with other
  unit types this estimate is wrong - says so in the code comment.
- [ ] **`farms.minimal_loss_tolerance`** (default `0.5`) - skip a B or C farm if the
  estimated average troop loss against the target's wall meets or exceeds this. Lower is
  more cautious.
- [ ] **`farms.report_freshness_hours`** (default `6`) / **`farms.report_max_age_hours`**
  (default `24`) - control the A/B/C decision: report older than `report_max_age_hours`
  (or missing) -> A (re-scout); older than `report_freshness_hours` but still within
  `report_max_age_hours` -> B (minimal); fresher than `report_freshness_hours` -> C
  (loot-exact, from report).
- [ ] **`farms.min_points` / `farms.max_points`** - point range of villages to farm
  (barbarians and player-owned alike, also gated by `farms.find_player_owned`).
- [ ] **`farms.search_radius`** - max distance (fields) from the village to look for farms.
- [ ] **`farms.find_player_owned`** - off by default; only enable if you actually want to
  farm player villages (skipped 23:00-08:00 either way).
- [ ] **`village.scout_first`** (per-village) - should be on so the A step actually runs.
