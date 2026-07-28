# TWB Code Review

Reviewed on branch `incoming-attack-tracking`. This is a report only — no code was changed.

## How a bot cycle works (mental map)

- **Entry point** `twb.py` → `main()` → `TWB.start()` → `TWB.run()`. `run()` is one big loop:
  builds a `WebWrapper` (session), loads villages, spins up two daemon threads
  (`incoming_poller`, `scheduled_attack_runner`), then loops forever:
  heartbeat → internet check → `get_overview()` → per-village `Village.run()` → sleep.
- **Session/requests** `core/request.py` (`WebWrapper`): one `requests.Session`, human-pacing
  sleeps (3–7 s), CSRF/`h` token scraping, cookie persistence to `cache/session.json`,
  captcha blocking, `reauth()` from `cache/cookies.txt`.
- **Per village** `game/village.py` (`Village.run()`) orchestrates, in order: resources →
  defence → quests → scavenge-unlock → builder → unit template → upgrades → snob → recruit →
  farming → gather → market → cache write.
- **Sub-managers** `game/`: `buildingmanager`, `troopmanager`, `attack` (farming),
  `attack_scheduler` (timed sends), `resources` (+ market/premium), `defence_manager`,
  `snobber`, `reports`, `incomings`, `map`, `simulator`, `hunter`.
- **State persistence** JSON files under `cache/` (per world under `worlds/<name>/cache/`):
  `session.json`, `heartbeat.json`, `managed/<vid>.json`, `attacks/`, `reports/`,
  `villages/` (map), `incomings/`, `scheduled_attacks.json`, `scavenge_log.json`,
  `troops_moving.json`, `world/`.
- **Dashboard** `webmanager/server.py` (Flask) + `webmanager/utils.py` (`DataReader`,
  `OverviewBuilder`, `AttackPlanner`, `DefenseOverview`, `BotManager`). Reads the same cache
  files, edits `config.json`, starts/stops the bot via `psutil`/`subprocess`.

**Largest modules:** `webmanager/utils.py` (1423), `webmanager/server.py` (1174),
`game/village.py` (847), `game/troopmanager.py` (812), `twb.py` (800), `game/incomings.py` (578),
`game/resources.py` (551), `game/attack.py` (513).

**Uncommitted changes (git status):** `core/request.py`, `game/village.py`, `twb.py`,
`webmanager/server.py`, `webmanager/utils.py`, `start.sh`, plus new dashboard templates and the
`browser-extension/`. So the actively-churned code is the session layer, the village
orchestration, the attack scheduler and the dashboard — which is also where most of the bugs below sit.

---

## 1. Bugs (ranked by severity)

_This audit was written in July 2026 and every entry has a **Status** line rechecked on
2026-07-28. Ten of the eleven are fixed; B6 is partly fixed and the remainder is a
labelling matter. The findings are kept for the reasoning, not because they are open._

### B1 — `WebWrapper` API helpers crash the whole village run on any failed request `core/request.py:266`, `:291`, `:316`

> Status (rechecked 2026-07-28): **FIXED** — all three helpers guard `res is not None` and the `recruit` call site checks the result.
`get_api_data`, `post_api_data` and `get_api_action` all do `if res.status_code == 200:` but
`res` can be `None`: `post_url`/`get_url` return `None` on any exception (`core/request.py:131`,
`:153`) or when the captcha page is returned. Calling `.status_code` on `None` raises
`AttributeError`.
- **Trigger:** a single transient network error (connection reset, timeout) during any
  recruit/research/farm/flag/market API call. `TroopManager.recruit` (`troopmanager.py:757`) then
  hits the exception, which bubbles out of `Village.run()`, aborts the whole cycle, and burns one
  of the 3 crash-restarts in `main()` (`twb.py:726`).
- **Also:** `recruit` at `troopmanager.py:763` does `if "game_data" in result:` — if
  `get_api_action` returns `None` (non-200 handled path) this is `argument of type 'NoneType' is
  not iterable`.
- **Fix:** guard `res` first in all three helpers: `if res is not None and res.status_code == 200:`
  and return `None` otherwise; make callers treat `None` as "action failed, retry next cycle."

### B2 — `ResourceManager.can_recruit` mutates a dict while iterating it `game/resources.py:257-259`

> Status (rechecked 2026-07-28): **FIXED** — deletes over a copy of the keys.
```python
for x in self.requested:
    if "recruitment" in x:
        del self.requested[x]
```
Deleting a key during `for x in self.requested` raises `RuntimeError: dictionary changed size
during iteration`.
- **Trigger:** population is full (`self.actual["pop"] == 0`) while at least one
  `recruitment_*` request is outstanding — a completely normal state once a village fills up.
  The exception propagates through `do_recruit` → `Village.run()` and kills the cycle.
- **Fix:** iterate a copy of the keys: `for x in [k for k in self.requested if "recruitment" in k]: del self.requested[x]`.

### B3 — "Forced peace today" is never detected (writes locals, not attributes) `game/village.py:359-360`

> Status (rechecked 2026-07-28): **FIXED** — assigns to `self.forced_peace_today` / `self.forced_peace_today_start`.
```python
if start_dt.date() == datetime.today().date():
    forced_peace_today = True          # local variable, not self.forced_peace_today
    forced_peace_today_start = start_dt # local variable, not self.forced_peace_today_start
```
The method sets local variables that are immediately discarded. `self.forced_peace_today` stays
`False` (reset at `:352`), so `run_farming` (`village.py:524`) never sets
`attack.forced_peace_time`, and `AttackManager.attack` (`attack.py:433`) never skips attacks that
would land during a forced-peace window.
- **Trigger:** configure a `farms.forced_peace_times` window for the current day; the bot still
  launches farms that arrive inside the peace window (against the whole point of the feature).
- **Fix:** assign to `self.forced_peace_today` / `self.forced_peace_today_start`.

### B4 — Crash before `self.wrapper` is set loses the notification and aborts all retries `twb.py:732`

> Status (rechecked 2026-07-28): **FIXED** — `if t.wrapper and t.wrapper.reporter:` before reporting.
In `main()`'s `except`, `t.wrapper.reporter.report(...)` runs unconditionally. If the exception
happened before `self.wrapper` was assigned (e.g. in `self.config()` at `twb.py:518`, or an
invalid-config raise), `t.wrapper` is still `None` → `AttributeError` inside the `except` block.
That secondary exception escapes the `for _ in range(3)` loop, so the bot neither retries nor
sends the "crashed" Telegram notification (`twb.py:734`).
- **Trigger:** corrupt/missing config or any early startup failure.
- **Fix:** guard `if t.wrapper and t.wrapper.reporter:` before reporting.

### B5 — `is_active_hours` can't express an overnight window and drops the last hour `twb.py:409-411`

> Status (rechecked 2026-07-28): **FIXED** — end bound inclusive, wrap-around handled, and `HH:MM` bounds now accepted.
```python
active_h = [int(h) for h in config["bot"]["active_hours"].split("-")]
return time.localtime().tm_hour in range(active_h[0], active_h[1])
```
- `range(a, b)` is end-exclusive, so `"6-23"` treats **23:00–23:59 as inactive** (off-by-one vs.
  the "6 to 23" a user reads).
- An overnight window like `"22-6"` produces `range(22, 6)` = empty, so the bot is considered
  **inactive 24/7** and only ever uses `inactive_delay`.
- **Trigger:** any user who wants an active window that spans midnight, or who expects hour 23 to
  be active.
- **Fix:** handle wrap-around (`start <= h or h < end` when `start > end`) and decide explicitly
  whether the end hour is inclusive; `_gather_night_consolidate` (`village.py:563-567`) already
  does the wrap-around correctly and can be the model.

### B6 — Timezone mismatch between scheduling/forced-peace (local) and the game (server) `game/village.py:355-357`, `webmanager/utils.py:313-321`, `game/attack_scheduler.py:259`

> Status (rechecked 2026-07-28): **PARTLY FIXED** — forced-peace windows are anchored to the server's own clock in both the bot and the dashboard (`core/server_clock.py`), and a timezone gap is now detected and warned about rather than being invisible. Timed attacks were never affected by a timezone difference (they wait on epoch time, which both sides agree on); a genuinely wrong host clock still offsets them, and is warned about. What remains is UX: arrival times typed into the dashboard are read in the browser's timezone, now labelled as such.
Forced-peace windows are parsed with naive `datetime.strptime`/`datetime.now()` (local host time),
and the dashboard's `_forced_peace_conflict` (`utils.py:313`) does the same. Scheduled-attack
timing (`attack_scheduler.execute_timed`, `:259`) mixes `time.time()` (host clock) with the
server's reported travel duration. Incoming detection, by contrast, uses **server** time
(`incomings.py:391` `_server_time`).
- **Trigger:** host TZ (or clock) differs from the TribalWars server’s. Forced-peace windows then
  start/end at the wrong wall-clock moment, and timed attacks land offset by the clock skew.
- **Fix:** anchor scheduling and forced-peace to server time (already available via
  `Extractor.game_state(...)["time_generated"]`), or document that the host clock must match the
  server TZ.

### B7 — Scheduled-attack claim can be held in `sending` far longer than intended `game/attack_scheduler.py:161`, `:264`

> Status (rechecked 2026-07-28): **FIXED** — a stale `sending` claim is reclaimable after `STALE_SENDING_SECONDS`.
`claim_due` flips a command to `sending` up to `PRESTAGE_SECONDS` (15 s) early, and then
`execute_timed` **sleeps** until the launch moment while the command is already claimed. If the bot
process dies during that window (crash, `stop`), the command is left permanently in `sending` and
is never retried (only `pending` is ever claimed again).
- **Trigger:** bot restart within the ~15 s pre-stage window of a queued command.
- **Fix:** treat a `sending` command whose `claimed_at` is older than a threshold as reclaimable in
  `claim_due`, or write status `sent/failed` before the final sleep.

### B8 — Reports parse loot/units without the "no report at all" guard `manager.py:50-53`

> Status (rechecked 2026-07-28): **FIXED** — `units_sent` / `units_losses` read through `.get()` with `{}` defaults.
`farm_manager` iterates `report["extra"]["units_sent"]` / `["units_losses"]` directly. Attack
reports normally have these, but `attack_report` only populates them when the attacker table and
its unit sub-tables are present (`reports.py:237-245`); a malformed or partial report leaves them
absent, so `report["extra"]["units_sent"]` is a `KeyError`. The `try/except` at `manager.py:54`
only wraps the **loot** access, not the two unit loops above it.
- **Trigger:** a report whose attacker unit table failed to parse (asset/markup change) → `KeyError`
  → `farm_manager` aborts mid-loop, so profile/safety flags stop being updated for the remaining farms.
- **Fix:** use `.get(..., {})` for `units_sent`/`units_losses`, or widen the `try` to cover them.

### B9 — `session_logged_out` false-positive wipes cycle when overview parse fails but session is alive `twb.py:255`, `pages/overview.py:298`

> Status (rechecked 2026-07-28): **FIXED** — `found_villages` is preserved when a logged-in page parses to zero villages.
`get_overview` decides logged-in purely from `Extractor.game_state(overview_page.result_get)`
against `overview_villages&mode=combined`. `parse_production_table` swallows every per-row
exception (`overview.py:298` `except Exception: pass`), so a markup change that breaks parsing
still yields a page *with* game state → logged-in True but `villages_data` empty → falls back to
regex `village_ids_from_overview`. If that regex also misses, `found_villages` becomes `[]` and
every village is reported "not available anymore" (`twb.py:635-639`) and skipped — silently doing
nothing while looking healthy.
- **Trigger:** any future change to the combined-overview table markup.
- **Fix:** distinguish "logged in but parsed zero villages" from "genuinely zero villages" — e.g.
  keep the previous `found_villages` when game state is present but the village list came back empty,
  the same way the logged-out path already preserves it (`twb.py:267-282`).

### B10 — `tw_proxy` reads the user agent from the wrong config section `webmanager/server.py:1047`

> Status (rechecked 2026-07-28): **FIXED** — reads `bot.user_agent`.
```python
ua = (config.get("server") or {}).get("user_agent", "Mozilla/5.0")
```
The user agent lives under `bot.user_agent` (`config.example.json:31`), not `server`. The proxy
therefore always sends the default `Mozilla/5.0`, defeating the whole "use your real browser UA to
lower detection" design for proxied requests.
- **Fix:** `(config.get("bot") or {}).get("user_agent", "Mozilla/5.0")`.

### B11 — Flask debug mode enabled with an all-interfaces bind `webmanager/server.py:27`, `:1171`, `start.sh:68`

> Status (rechecked 2026-07-28): **FIXED** — `app.config["DEBUG"] = False`; debug is enabled only for local binds, and a non-local bind logs a warning. The dashboard still has no authentication by design — see the README.
`app.config["DEBUG"] = True` (and the implicit reloader/console) combined with `start.sh` binding
`HOST=0.0.0.0` exposes the Werkzeug interactive debugger, which allows **remote code execution** on
any unhandled exception. The dashboard also has no authentication.
- **Trigger:** dashboard reachable on a LAN/VPS with any 500-triggering request.
- **Fix:** turn off debug for any non-local bind, and/or require the panel to bind `127.0.0.1`
  unless explicitly protected.

---

## 2. Code quality improvements

- **[DEFERRED — do later] Three (four) near-identical attack-form/confirm sequences.** `AttackManager.attack`
  (`attack.py:406-462`), `DefenceManager.support` (`defence_manager.py:267-313`) and
  `attack_scheduler.prepare_command` (`attack_scheduler.py:190-225`) each re-implement: open rally
  point → `Extractor.attack_form` → post `try=confirm` → check `error_box` → rebuild confirm data →
  `popup_command`. (The dead `game/hunter.py` `attack`/`prepare` is a 4th copy.) Extract one helper
  (open/confirm/launch) and have all call it. Deferred: refactors the critical attack path and needs a
  live game session to exercise safely. The confirm-data key sub-bug (`defence_manager.py:304`, coord
  value used as dict key vs. the correct string `"x"`) is ALREADY FIXED separately.
- **`Village.run` is a 50-line straight-line orchestration with duplicate guards.** `village.py:703`
  and `:719` both check `if not self.game_data` (the second is dead — the first already raised).
  The `vdata = self.get_config("villages", self.village_id)` at `:714` re-does the lookup from `:711`.
- **Dead / deprecated code.** `incomings.suggest_tag` (`incomings.py:94-108`) is documented as
  deprecated and unused (only `slowest_floor` is called) — REMOVED. `Map.get_map_old`
  (`map.py:80-112`) is a legacy fallback with a bare `except: raise` — kept (still called at
  `map.py:77`). `core/twstats.py` — kept (used via `server_on_twstats`). `Extractor.get_daily_reward`
  (`extractors.py:302`) — kept as scaffolding for the section-4 daily-bonus feature.
- **[TODO — keep for later] `game/hunter.py`.** WIP prototype for timed, coordinated multi-attack
  chains ("trains"/snipes); not imported anywhere and never ran (undefined `self.village_id`,
  `self.map` vs `self.game_map`, wrong sleep units). Superseded for now by `attack_scheduler.py` but
  intentionally retained to revive later — see the TODO header in the file. Do NOT delete.
- **Config options that nothing reads.** `village_template.scout_first`
  (`config.example.json:61`; only a helpfile string exists, no code path), `farms.find_player_owned`
  (`config.example.json:91`; documented in `helpfile.py:47` but never read — farming instead uses
  `additional_farms`), and `village_template.support_others_max_villages`
  (`config.example.json:83`; `DefenceManager.support_max_villages` is hardcoded at
  `defence_manager.py:41` and never populated from config). Either wire them up or remove them so
  the UI stops implying they work.
- **Hardcoded values that belong in config.** `internet_online` probes `https://www.google.com`
  (`twb.py:125`); the "no player-owned farms 23h–8h" window is hardcoded (`attack.py:362-363`);
  the "no trading 23h–6h" window is hardcoded (`resources.py:404`); `scout_wait = 900`,
  `light_cavalry_load = 80`, farm-run interval `1500–2700` are class constants in `attack.py:63-69`.
- **Inconsistent error handling.** Broad `except Exception: pass` appears in
  `update_troop_movements` (`twb.py:328`), `_capture_label_endpoint` (`incomings.py:387`),
  `parse_production_table` (`overview.py:298`), and several dashboard reads — these hide real
  parsing regressions. At minimum log at debug level with the exception.
- **`manage_market` logging bug.** `resources.py:445-448` sets `how_many = self.max_trade_amount`
  and *then* logs `"Lowering trade amount of %d to %d"` with `(how_many, self.max_trade_amount)`,
  so both numbers print identically. Log before the reassignment.

## 3. Performance / request-pattern

- **[FIXED] Two extra full page fetches every main cycle for dashboard-only data.**
  `update_troop_movements` (`twb.py`) GETs `mode=units&type=moving` and `type=away` on every
  overview, purely to feed the dashboard troop split. Now throttled to at most once per
  `TROOP_MOVE_REFRESH_SECONDS` (900s) via the timestamp already in `troops_moving.json`, so it no
  longer runs the two GETs every cycle.
- **[DEFERRED — needs live testing] `Village.run` fetches the overview/main page several times per
  village.** `village_init` (`overview`), `update_totals` (`overview` again, `troopmanager.py:119`),
  `run_builder`'s `start_update` (`main`, twice — before and after queueing), and `go_manage_market`
  re-GETs `overview`. Some could reuse one `game_state` per cycle, but a few of these intentionally
  re-fetch fresh state *after* an action (e.g. re-reading `main` after queueing a build), so
  collapsing them blindly risks acting on stale state. Same class as the helper extraction: restructures
  the critical per-village path, needs a live session to verify.
- **[FIXED] Reports are re-read from disk every dashboard build.** `OverviewBuilder.build`'s
  full-report scan for 24h/all-time counters is now memoised (`_farm_trade_records`, keyed on a cheap
  `_reports_signature` = reports-dir path + file count + newest mtime). The O(all reports) JSON parse
  only reruns when a report is actually added/changed; the windowed sums stay in-memory and
  second-accurate.
- **[SKIPPED — not worth it] Farm target selection recomputes the distance filter each farm run.**
  `AttackManager.get_targets` (`attack.py:314`). Investigated: `get_dist` is a single `math.sqrt`
  (negligible), the result is time-dependent (the 23h–8h player-owned window) and config/state-dependent
  (points thresholds, mutable ignore list) so a memo would be fragile, and the real per-run cost —
  `fetch_farm_icons`' `am_farm` GET — is intentionally-live troop/wall data that *must* be re-fetched.
  Left as-is (review itself notes it's "fine at the interval").
- **[ACCEPTED as-is] Polling threads instead of event-driven waits.** `scheduled_attack_runner`
  busy-waits with `time.sleep(2)`. Review already marks this "Lower priority"; the 2s wakeup is cheap
  and event-driven signalling adds complexity for little gain.
- **[FIXED] Bot-detection risk — request bursts.** Timed sends run under `priority_mode`, which strips
  the 3–7s pacing. Now `prepare_command` adds a randomized 0.4–1.8s gap between the open and confirm
  steps (only under `priority_mode`), inside the pre-stage window, so open/confirm no longer fire
  back-to-back while the final launch stays instant and time-accurate.

## 4. Missing / half-implemented features

| Feature | Where it hooks in | Evidence | Effort |
|---|---|---|---|
| `scout_first` per-village behaviour | `Village.run_farming` / `AttackManager` | config key + helpfile exist, no code reads it | Medium |
| `find_player_owned` auto-farming | `AttackManager.get_targets` (`attack.py:326`) | documented (`helpfile.py:47`) but only `additional_farms` is honoured | Medium |
| `support_others_max_villages` from config | `DefenceManager.support_max_villages` (`defence_manager.py:41`) | hardcoded to 2; config value never loaded in `setup_defence_manager` | Small |
| Premium build-cost reduction | `BuildingManager.complete_actions` (`buildingmanager.py:153`) | explicit `TODO: add premium options to lower build costs` | Medium |
| ~~Compiled regexes in `Extractor`~~ **[DONE]** | `core/extractors.py` | All 31 patterns precompiled at module level; verified verbatim vs original + identical output on 8 parsers | Small |
| ~~Daily bonus collection~~ **[DONE]** | `game/dailybonus.py`, hooked in `twb.py` behind `bot.claim_daily_bonus` | Claims unlocked+uncollected chests once/day during active hours via `ajaxaction=open` (payload verified against the game's own `DailyBonus.openChest`). The old inverted/crashy `get_daily_reward` extractor was replaced by `daily_bonus_data` (raw_decode of the real init call). | Medium |
| Bot "stall" auto-recovery | `WebWrapper.get_url` captcha path (`request.py:110-127`) | it blocks on `input()` forever in a headless run and only writes `captcha_block.json`; nothing auto-restarts or alerts beyond a Telegram message | Medium |
| ~~Shell/log dashboard page backend~~ **[NOT NEEDED]** | — | Misread: `shell.html` is the base layout every page `{% extends %}`, not a standalone page; `/logs` route already exists (`server.py:1127`). No action. | — |
| ~~`webmanager/public/js.v2.js`~~ **[DONE]** | — | `/app/js` was dead (no template references it; JS is inlined in `shell.html`). Removed the broken route + now-unused `send_from_directory` import instead of fabricating a phantom file. | Small |
| Attack scheduler retry of stuck `sending` | `attack_scheduler.claim_due` | see B7 | Small |

## 5. Top 5 recommendations (in order)

1. **Harden `WebWrapper` API helpers against `None` responses (B1).** This is the single most
   likely thing to kill a live run: one dropped request currently crashes the village cycle and eats
   a restart. Guard `res` in `get_api_data`/`post_api_data`/`get_api_action` and make callers treat a
   failed action as "retry next cycle."
2. **Fix `can_recruit`'s dict-mutation-during-iteration crash (B2).** Trivial change, triggers on a
   normal full-population state, and takes down recruiting for the whole cycle.
3. **Fix the forced-peace bugs (B3 + B6).** Assign to `self.` in `check_forced_peace`, then anchor
   both the bot and the dashboard's forced-peace/scheduling math to **server** time. Today the
   feature silently does nothing (B3) and, even once fixed, is off by the host clock skew (B6) — a
   real risk of attacking during a no-attack event and getting the account flagged.
4. **Make village availability robust to overview-parse failures (B9) and fix the active-hours
   window (B5).** Both are "the bot looks healthy but quietly stops doing its job" failures: an
   overview markup change parks every village as unavailable, and any overnight active window pins
   the bot to inactive delays 24/7.
5. **Lock down the dashboard (B11) and de-burst timed sends (perf §3).** Disable Flask debug on
   non-local binds and add auth before exposing `0.0.0.0`; add jitter to the scheduled-attack
   open/confirm requests so `priority_mode` doesn't emit an unnatural three-request burst.
